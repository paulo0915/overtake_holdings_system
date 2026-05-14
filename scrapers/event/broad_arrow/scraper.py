"""Broad Arrow Auctions — session-aware, event-based scraper.

Discovery path (matches real browser navigation to satisfy reCAPTCHA v3):
    GET /                    warm up session / cookies
    GET /past-auctions       → list of /events/event/{name} URLs
    GET /events/event/{name} → branch-specific results URL (branch_id_eq=N)
    GET /vehicles/results?branch_id_eq=N&page=K → vehicle paths + rel=next
    GET /vehicles/{lot}/{slug} → detail (JSON-LD Car schema)

--max-pages N  = process at most N events (0 = all events, full history)
"""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from . import record_builder
from .api_client import (
    branch_results_page_url,
    fetch_branch_results_html,
    fetch_event_html,
    fetch_event_meta_html,
    fetch_past_auctions_html,
    fetch_upcoming_vehicles_html,
    fetch_vehicle_html,
    fetch_warmup,
)
from .config import SITE_ORIGIN
from .listing_parser import (
    extract_event_slug,
    max_card_price_usd_from_results,
    next_results_page_url,
    parse_branch_results_url_from_event,
    parse_event_meta_from_html,
    parse_event_paths_from_past_auctions,
    parse_vehicle_detail,
    parse_vehicle_paths_from_results,
)

logger = logging.getLogger(__name__)


class BroadArrowScraper(BaseScraper):
    """Past results or upcoming lots from [Broad Arrow Auctions](https://www.broadarrowauctions.com)."""

    SOURCE_NAME = "Broad Arrow"
    SOURCE_DOMAIN = "broadarrowauctions.com"
    DATABASE = "auctions"

    def __init__(
        self,
        min_price: int = 0,
        output_dir="output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter=None,
        mode: str = "past",
    ):
        super().__init__(
            min_price=min_price,
            output_dir=output_dir,
            max_concurrent=max_concurrent,
            max_pages=max_pages,
            vehicle_filter=vehicle_filter,
        )
        if mode not in {"past", "upcoming"}:
            raise ValueError(f"mode must be 'past' or 'upcoming', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Broad Arrow - Starting (mode=%s)", self.mode)
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_events = self.max_pages  # 0 = unlimited
        if max_events > 0:
            unit = "listing page" if self.mode == "upcoming" else "event"
            logger.info("Max %ss (test mode): %s", unit, max_events)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # --- Step 1: warm up session (sets cookies for reCAPTCHA) --------
            try:
                await fetch_warmup(client)
                logger.info("Session warmed up (GET /)")
            except Exception as e:
                logger.warning("Warmup failed (continuing anyway): %s", e)

            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.1, 0.4))

            # --- Step 1b: second warmup GET /vehicles — mimics clicking the
            # "Vehicles" nav before deep-linking into detail pages. Without this,
            # ~30%+ of lot detail GETs return HTTP 202 Cloudflare challenges.
            try:
                from .api_client import fetch_upcoming_vehicles_html as _warm2
                await _warm2(client, page=1)
                logger.info("Secondary warmup (GET /vehicles)")
            except Exception as e:
                logger.debug("Secondary warmup failed: %s", e)
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.3 + random.uniform(0.1, 0.3))

            # --- Step 2: discover vehicle paths + collect event metadata ------
            if self.mode == "upcoming":
                all_paths, event_meta = await self._discover_upcoming(client, max_events)
            else:
                all_paths, event_meta = await self._discover_past(client, max_events)

            logger.info("Collected %s unique vehicle paths — fetching details", len(all_paths))
            logger.info("Event metadata cache: %s slug(s): %s", len(event_meta), list(event_meta))

            # --- Step 3: concurrent detail fetches ---------------------------
            detail_conc = max(1, min(self.max_concurrent, 5))
            sem = asyncio.Semaphore(detail_conc)
            lock = asyncio.Lock()
            details: dict[str, dict | None] = {}
            done = 0
            total = len(all_paths)

            async def one(path: str) -> None:
                nonlocal done
                async with sem:
                    h = await fetch_vehicle_html(client, vehicle_path=path)
                    parsed = parse_vehicle_detail(h, vehicle_path=path, mode=self.mode)
                    async with lock:
                        details[path] = parsed
                        done += 1
                        if done % 25 == 0 or done == total:
                            logger.info("Detail fetch %s/%s", done, total)
                await asyncio.sleep(
                    REQUEST_DELAY_SECONDS * 0.25 + random.uniform(0.03, 0.2)
                )

            await asyncio.gather(*(one(p) for p in all_paths))

            rows = [details[p] for p in all_paths if details.get(p)]

            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at, mode=self.mode,
                event_meta=event_meta,
            )

            before = len(records)
            if self.mode == "past":
                records = [r for r in records if self._row_meets_min_price(r)]
                logger.info(
                    "After min-price ($%s): %s -> %s records",
                    f"{self.min_price:,}", before, len(records),
                )
            else:
                logger.info("Upcoming mode — %s records (min-price filter skipped)", before)

        suffix = "upcoming" if self.mode == "upcoming" else "auction_results"
        csv_path = self.output_dir / f"broad_arrow_{suffix}.csv"
        xlsx_path = self.output_dir / f"broad_arrow_{suffix}.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Broad Arrow - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    async def _discover_past(
        self, client, max_events: int
    ) -> tuple[list[str], dict[str, dict]]:
        """Original event-driven discovery: /past-auctions → per-event branch results.

        Returns ``(vehicle_paths, event_meta_cache)`` where ``event_meta_cache`` maps
        event slug → ``{"country": ..., "currency": ..., "date": ...}``.
        """
        all_paths: list[str] = []
        seen_paths: set[str] = set()
        event_meta: dict[str, dict] = {}

        try:
            pa_html = await fetch_past_auctions_html(client)
        except Exception as e:
            logger.error("Failed to fetch /past-auctions: %s", e)
            return [], {}

        event_paths = parse_event_paths_from_past_auctions(pa_html)
        logger.info("Found %s auction events on /past-auctions", len(event_paths))

        if max_events > 0:
            event_paths = event_paths[:max_events]
            logger.info("Limited to %s event(s) for test run", max_events)

        for ev_idx, event_path in enumerate(event_paths, start=1):
            event_url = SITE_ORIGIN + event_path
            # Extract slug from path like "/events/event/ve26"
            slug = event_path.rstrip("/").rsplit("/", 1)[-1].lower()
            logger.info("[%s/%s] Event: %s (slug=%s)", ev_idx, len(event_paths), event_path, slug)
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.6 + random.uniform(0.1, 0.5))

            try:
                ev_html = await fetch_event_html(client, event_path)
            except Exception as e:
                logger.warning("Failed event page %s: %s — skipping", event_path, e)
                continue

            # Parse event metadata (country/currency/date) from the event page HTML
            if slug and slug not in event_meta:
                meta = parse_event_meta_from_html(ev_html)
                event_meta[slug] = meta
                logger.info(
                    "  Event meta: country=%s currency=%s date=%s",
                    meta.get("country"), meta.get("currency"), meta.get("date"),
                )

            branch_base = parse_branch_results_url_from_event(ev_html)
            if not branch_base:
                logger.warning("No branch results URL on %s — skipping", event_path)
                continue

            logger.info("  Branch results base: %s", branch_base)
            page = 1
            event_paths_count = 0
            while True:
                page_url = branch_results_page_url(branch_base, page)
                await asyncio.sleep(
                    (REQUEST_DELAY_SECONDS * 0.4 if page > 1 else 0)
                    + random.uniform(0.05, 0.3)
                )
                try:
                    res_html = await fetch_branch_results_html(
                        client, url=page_url, event_url=event_url,
                    )
                except Exception as e:
                    logger.warning("  Results page %s for %s failed: %s",
                                   page, event_path, e)
                    break
                paths = parse_vehicle_paths_from_results(res_html)
                new = 0
                for p in paths:
                    if p not in seen_paths:
                        seen_paths.add(p)
                        all_paths.append(p)
                        new += 1
                event_paths_count += len(paths)
                logger.info("  Results page %s: %s on page (%s new), %s unique total",
                            page, len(paths), new, len(all_paths))
                if not paths or not next_results_page_url(res_html):
                    break
                # Early-stop: results are sorted ``sales_price.desc`` and prices
                # on the cards are pre-converted to USD by Broad Arrow's
                # ``convert_price`` widget — so once the highest USD price on a
                # full page falls below the floor every later page in this
                # branch is below it too. Skips the per-vehicle detail crawl
                # for tens of pages of cheap lots when ``min_price`` is set.
                if self.min_price > 0:
                    page_max = max_card_price_usd_from_results(res_html)
                    if page_max is not None and page_max < self.min_price:
                        logger.info(
                            "  Page %s top USD price ($%s) < floor ($%s) - "
                            "early stop (skipping deeper pages of this branch)",
                            page, f"{page_max:,}", f"{self.min_price:,}",
                        )
                        break
                page += 1
            logger.info("  Event done: %s vehicle paths across %s result page(s)",
                        event_paths_count, page)
        return all_paths, event_meta

    async def _discover_upcoming(
        self, client, max_pages: int
    ) -> tuple[list[str], dict[str, dict]]:
        """Paginate ``/vehicles`` — defaults to upcoming-only, all active branches.

        Returns ``(vehicle_paths, event_meta_cache)`` where ``event_meta_cache`` maps
        event slug → ``{"country": ..., "currency": ..., "date": ...}``.
        After collecting all paths, fetches the event metadata page once per unique
        slug so that country/currency/date can be stored per lot.
        """
        all_paths: list[str] = []
        seen_paths: set[str] = set()
        page = 1
        while True:
            try:
                html = await fetch_upcoming_vehicles_html(client, page=page)
            except Exception as e:
                logger.warning("Upcoming /vehicles?page=%s failed: %s", page, e)
                break
            paths = parse_vehicle_paths_from_results(html)
            if not paths:
                break
            new = 0
            for p in paths:
                if p not in seen_paths:
                    seen_paths.add(p)
                    all_paths.append(p)
                    new += 1
            logger.info("Upcoming page %s: %s on page (%s new), %s unique total",
                        page, len(paths), new, len(all_paths))
            if new == 0:
                break
            if max_pages > 0 and page >= max_pages:
                logger.info("Limited to %s upcoming listing page(s)", max_pages)
                break
            page += 1
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.35 + random.uniform(0.05, 0.25))

        # Collect unique event slugs and fetch event metadata pages (one per slug).
        event_meta_cache: dict[str, dict] = {}
        unique_slugs: list[str] = []
        seen_slugs: set[str] = set()
        for p in all_paths:
            slug = extract_event_slug(p)
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                unique_slugs.append(slug)

        for slug in unique_slugs:
            try:
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.3 + random.uniform(0.05, 0.2))
                meta_html = await fetch_event_meta_html(client, slug)
                meta = parse_event_meta_from_html(meta_html)
                event_meta_cache[slug] = meta
                logger.info(
                    "Event meta [%s]: country=%s currency=%s date=%s",
                    slug, meta.get("country"), meta.get("currency"), meta.get("date"),
                )
            except Exception as e:
                logger.warning("Could not fetch event meta for slug %r: %s", slug, e)
                event_meta_cache[slug] = {"country": None, "currency": None, "date": None}

        return all_paths, event_meta_cache

    def _row_meets_min_price(self, row: dict) -> bool:
        usd = row.get("Selling_price_USD")
        if usd is None:
            usd = row.get("Bid_price_USD")
        if usd is None:
            return self.min_price <= 0
        try:
            return int(usd) >= self.min_price
        except (TypeError, ValueError):
            return False
