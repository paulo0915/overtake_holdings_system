"""Sotheby's Motorsport — sold auctions (listings API + per-listing pageProps enrichment)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random

import httpx
from curl_cffi.requests import AsyncSession

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from . import api_client, record_builder
from .config import DEFAULT_PAGE_SIZE, SORT_BID_HIGH, TYPE_SOLD
from .listing_parser import merge_detail_page_props, parse_summary_from_api_row

logger = logging.getLogger(__name__)


class SothebysMotorsportScraper(BaseScraper):
    """Ended Sotheby's Motorsport auctions — ``type=sold``, ``sort=bid_high``."""

    SOURCE_NAME = "Sotheby's Motorsport"
    SOURCE_DOMAIN = "sothebysmotorsport.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = self.output_dir / ".sothebys_motorsport_checkpoint.json"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Sotheby's Motorsport — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max API pages: %s (test mode)", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = self._load_checkpoint()
        cached_rows = checkpoint.get("rows", []) if checkpoint else []
        cached_details = checkpoint.get("details", {}) if checkpoint else {}

        email = os.environ.get("SOTHEBYS_MOTORSPORT_EMAIL", "").strip()
        password = os.environ.get("SOTHEBYS_MOTORSPORT_PASSWORD", "")

        # Load any previously saved session cookies so we can skip re-login.
        saved_cookies = api_client.load_session_cache(self.output_dir)

        records: list[dict] = []

        # curl_cffi impersonates Chrome 124's TLS fingerprint, bypassing
        # Cloudflare's JA3/JA4 bot detection that blocks plain httpx.
        async with AsyncSession(
            impersonate="chrome124",
            cookies=saved_cookies if saved_cookies else None,
        ) as session:

            # --- session management ---
            if saved_cookies:
                if await api_client.validate_session(session):
                    logger.info("Reusing cached NextAuth session — no login needed.")
                else:
                    logger.info("Cached session expired — logging in fresh.")
                    session.cookies.clear()
                    saved_cookies = {}

            if not saved_cookies:
                if email and password:
                    try:
                        await api_client.login_with_credentials(session, email, password)
                        logger.info("Authenticated (NextAuth session cookies set).")
                        api_client.save_session_cache(self.output_dir, session)
                    except api_client.SothebysAuthError:
                        logger.exception(
                            "Login failed; check SOTHEBYS_MOTORSPORT_EMAIL / "
                            "SOTHEBYS_MOTORSPORT_PASSWORD."
                        )
                        raise
                elif email or password:
                    logger.warning(
                        "Provide both SOTHEBYS_MOTORSPORT_EMAIL and "
                        "SOTHEBYS_MOTORSPORT_PASSWORD for login."
                    )
                else:
                    logger.info("No Sotheby's credentials set — scraping without auth.")

            # --- build ID discovery ---
            build_id = await api_client.get_build_id(session)
            if build_id:
                logger.info("Next.js build ID: %s", build_id)
            else:
                logger.warning("Could not determine Next.js build ID; falling back to HTML.")

            # --- listing summaries ---
            if cached_rows:
                summaries = cached_rows
                logger.info("Resumed %s listing summaries from checkpoint", len(summaries))
            else:
                summaries = await self._collect_listing_summaries(session)
                logger.info("Collected %s listings >= $%s", len(summaries), f"{self.min_price:,}")

            if not summaries:
                logger.warning("No listings matched criteria.")
            else:
                details = await self._fetch_all_page_props(
                    session, summaries, cached_details, build_id=build_id,
                )

                merged = [
                    merge_detail_page_props(dict(s), details.get(s["slug"]))
                    for s in summaries
                ]

                # FX conversion uses a plain httpx client (Frankfurter API has no bot protection).
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
                    fx = FXConverter(fx_client)
                    scraped_at = get_scraped_at()
                    records = await record_builder.build_records(
                        merged, fx, self.vehicle_filter, scraped_at,
                    )
                logger.info("Built %s records", len(records))

        csv_path = self.output_dir / "sothebys_motorsport_auction_results.csv"
        excel_path = self.output_dir / "sothebys_motorsport_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        self._delete_checkpoint()
        logger.info("=" * 60)
        logger.info("Sotheby's Motorsport — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    # ── Listing pagination ───────────────────────────────────────────────────

    async def _collect_listing_summaries(self, session: AsyncSession) -> list[dict]:
        out: list[dict] = []
        page_num = 1
        limit = DEFAULT_PAGE_SIZE

        while True:
            logger.info("Listings API page %s …", page_num)
            try:
                payload = await api_client.fetch_listings_page(
                    session, page_num, limit, listing_type=TYPE_SOLD, sort=SORT_BID_HIGH,
                )
            except api_client.SothebysApiError as e:
                logger.warning("Listings page %s API error: %s", page_num, e)
                break
            except Exception as e:
                logger.warning("Listings page %s error: %s", page_num, e)
                break

            if not isinstance(payload, dict):
                logger.warning("Unexpected listings response on page %s", page_num)
                break
            if payload.get("success") is False:
                logger.warning("Listings API returned success=false on page %s", page_num)
                break

            data = payload.get("data") or {}
            auctions = data.get("auctions") or []
            if not auctions:
                logger.info("No auctions on page %s — done.", page_num)
                break

            below_threshold = False
            for raw in auctions:
                if not isinstance(raw, dict):
                    continue
                bid = (raw.get("bidDetails") or {}).get("value")
                try:
                    bid_i = int(bid) if bid is not None else 0
                except (TypeError, ValueError):
                    bid_i = 0
                if bid_i >= self.min_price:
                    out.append(parse_summary_from_api_row(raw))
                else:
                    below_threshold = True
                    break

            if below_threshold:
                logger.info("Reached bids below $%s — stopping pagination.", f"{self.min_price:,}")
                break
            if self.max_pages > 0 and page_num >= self.max_pages:
                logger.info("Max pages (%s) reached.", self.max_pages)
                break

            pg = (data.get("pagination") or {})
            page_count = pg.get("pageCount")
            if isinstance(page_count, int) and page_num >= page_count:
                break

            page_num += 1
            await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.2, 0.9))

        return out

    # ── Detail page fetching ─────────────────────────────────────────────────

    async def _fetch_all_page_props(
        self,
        session: AsyncSession,
        summaries: list[dict],
        cached: dict[str, dict | None],
        *,
        build_id: str | None = None,
    ) -> dict[str, dict | None]:
        details: dict[str, dict | None] = dict(cached)
        pending = [s for s in summaries if s.get("slug") and s["slug"] not in details]
        if not pending:
            return details

        sem = asyncio.Semaphore(self.max_concurrent)
        method = "_next/data JSON" if build_id else "HTML"
        logger.info(
            "Fetching %s detail pages via %s (%s cached)…",
            len(pending), method, len(details),
        )

        async def one(row: dict) -> tuple[str, dict | None]:
            slug = row["slug"]
            async with sem:
                try:
                    pp = await api_client.fetch_detail_page_props(session, slug, build_id=build_id)
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.5))
                    return slug, pp
                except Exception as e:
                    logger.warning("Detail fetch failed for %s: %s", slug, e)
                    return slug, None

        tasks = [asyncio.create_task(one(s)) for s in pending]
        done = 0
        for coro in asyncio.as_completed(tasks):
            slug, pp = await coro
            details[slug] = pp
            done += 1
            if done % 10 == 0 or done == len(tasks):
                self._save_checkpoint(summaries, details)
                logger.info("Detail progress %s/%s", done, len(tasks))

        return details

    # ── Checkpoint ───────────────────────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, rows: list[dict], details: dict[str, dict | None]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"rows": rows, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
