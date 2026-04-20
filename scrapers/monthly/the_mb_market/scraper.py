"""The MB Market — auction results (AJAX list + per-listing HTML)."""

from __future__ import annotations

import asyncio
import json
import logging
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
from .config import INITIAL_LAST_PAGE_PLACEHOLDER, LIST_TYPE_RESULTS, RESULTS_REFERER, DEFAULT_PAGE_SIZE
from .listing_parser import merge_summary, parse_detail_html, parse_listing_boxes

logger = logging.getLogger(__name__)


class TheMbMarketScraper(BaseScraper):
    """Ended auctions from [The MB Market](https://thembmarket.com/browse-auctions/results)."""

    SOURCE_NAME = "The MB Market"
    SOURCE_DOMAIN = "thembmarket.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = self.output_dir / ".the_mb_market_checkpoint.json"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("The MB Market — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max AJAX pages: %s (test mode)", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = self._load_checkpoint()
        cached_cards = checkpoint.get("cards", []) if checkpoint else []
        cached_details = checkpoint.get("details", {}) if checkpoint else {}

        records: list[dict] = []

        async with AsyncSession(impersonate="chrome124") as session:
            await session.get(RESULTS_REFERER, timeout=float(REQUEST_TIMEOUT))

            if cached_cards:
                cards = cached_cards
                logger.info("Resumed %s listing cards from checkpoint", len(cards))
            else:
                raw_cards = await self._collect_listing_cards(session)
                cards = self._filter_by_min_price(raw_cards)
                logger.info(
                    "Fetched %s auction cards site-wide; %s meet min price >= $%s",
                    len(raw_cards),
                    len(cards),
                    f"{self.min_price:,}",
                )

            if not cards:
                logger.warning("No listings matched criteria or AJAX returned empty.")
            else:
                details = await self._fetch_all_details(session, cards, cached_details)

                merged = [merge_summary(dict(c), details.get(c["slug"], {})) for c in cards]

                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
                    fx = FXConverter(fx_client)
                    scraped_at = get_scraped_at()
                    records = await record_builder.build_records(
                        merged, fx, self.vehicle_filter, scraped_at,
                    )
                logger.info("Built %s records", len(records))

        csv_path = self.output_dir / "the_mb_market_auction_results.csv"
        excel_path = self.output_dir / "the_mb_market_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        self._delete_checkpoint()
        logger.info("=" * 60)
        logger.info("The MB Market — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    def _filter_by_min_price(self, raw_cards: list[dict]) -> list[dict]:
        """The site has no server-side price sort — filter after full list fetch."""
        if self.min_price <= 0:
            return list(raw_cards)
        out: list[dict] = []
        for c in raw_cards:
            amt = c.get("amount_usd")
            if amt is None:
                continue
            if amt >= self.min_price:
                out.append(c)
        return out

    async def _collect_listing_cards(self, session: AsyncSession) -> list[dict]:
        """Paginate every AJAX results page and return *all* cards (no price filter)."""
        out: list[dict] = []
        last_page = INITIAL_LAST_PAGE_PLACEHOLDER
        page = 1
        limit = DEFAULT_PAGE_SIZE

        while True:
            try:
                payload = await api_client.fetch_auction_list_page(
                    session,
                    page=page,
                    last_page=last_page,
                    limit=limit,
                    listing_type=LIST_TYPE_RESULTS,
                )
            except api_client.MbMarketApiError as e:
                logger.warning("List page %s error: %s", page, e)
                break

            data = payload.get("data") or {}
            html = data.get("html") or ""
            lp = data.get("last_page")
            if isinstance(lp, int) and lp > 0:
                last_page = lp

            logger.info("AJAX listing page %s of %s …", page, last_page)

            batch = parse_listing_boxes(html)
            if not batch:
                logger.info("No cards on page %s — done.", page)
                break

            for card in batch:
                if card.get("slug"):
                    out.append(card)

            if self.max_pages > 0 and page >= self.max_pages:
                logger.info("Max pages (%s) reached (test mode).", self.max_pages)
                break

            if page >= last_page:
                break

            page += 1
            await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.2, 0.8))

        return out

    async def _fetch_all_details(
        self,
        session: AsyncSession,
        cards: list[dict],
        cached: dict[str, dict],
    ) -> dict[str, dict]:
        details: dict[str, dict] = dict(cached)
        pending = [c for c in cards if c.get("slug") and c["slug"] not in details]
        if not pending:
            return details

        sem = asyncio.Semaphore(self.max_concurrent)
        logger.info("Fetching %s detail pages (%s cached)…", len(pending), len(details))

        async def one(card: dict) -> tuple[str, dict]:
            slug = card["slug"]
            async with sem:
                try:
                    html = await api_client.fetch_listing_html(session, slug)
                    d = parse_detail_html(html)
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.05, 0.35))
                    return slug, d
                except Exception as e:
                    logger.warning("Detail fetch failed for %s: %s", slug, e)
                    return slug, {}

        tasks = [asyncio.create_task(one(c)) for c in pending]
        done = 0
        for coro in asyncio.as_completed(tasks):
            slug, d = await coro
            details[slug] = d
            done += 1
            if done % 10 == 0 or done == len(tasks):
                self._save_checkpoint(cards, details)
                logger.info("Detail progress %s/%s", done, len(tasks))

        return details

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, cards: list[dict], details: dict[str, dict]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"cards": cards, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
