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
    fetch_past_auctions_html,
    fetch_vehicle_html,
    fetch_warmup,
)
from .config import SITE_ORIGIN
from .listing_parser import (
    next_results_page_url,
    parse_branch_results_url_from_event,
    parse_event_paths_from_past_auctions,
    parse_vehicle_detail,
    parse_vehicle_paths_from_results,
)

logger = logging.getLogger(__name__)


class BroadArrowScraper(BaseScraper):
    """Past auction results from [Broad Arrow Auctions](https://www.broadarrowauctions.com)."""

    SOURCE_NAME = "Broad Arrow"
    SOURCE_DOMAIN = "broadarrowauctions.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Broad Arrow - Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_events = self.max_pages  # 0 = unlimited
        if max_events > 0:
            logger.info("Max events (test mode): %s", max_events)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        all_paths: list[str] = []
        seen_paths: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # --- Step 1: warm up session (sets cookies for reCAPTCHA) --------
            try:
                await fetch_warmup(client)
                logger.info("Session warmed up (GET /)")
            except Exception as e:
                logger.warning("Warmup failed (continuing anyway): %s", e)

            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.1, 0.4))

            # --- Step 2: discover event pages --------------------------------
            try:
                pa_html = await fetch_past_auctions_html(client)
            except Exception as e:
                logger.error("Failed to fetch /past-auctions: %s", e)
                return []

            event_paths = parse_event_paths_from_past_auctions(pa_html)
            logger.info("Found %s auction events on /past-auctions", len(event_paths))

            if max_events > 0:
                event_paths = event_paths[:max_events]
                logger.info("Limited to %s event(s) for test run", max_events)

            # --- Step 3: per-event → branch results → vehicle paths ----------
            for ev_idx, event_path in enumerate(event_paths, start=1):
                event_url = SITE_ORIGIN + event_path
                logger.info("[%s/%s] Event: %s", ev_idx, len(event_paths), event_path)

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.6 + random.uniform(0.1, 0.5))

                try:
                    ev_html = await fetch_event_html(client, event_path)
                except Exception as e:
                    logger.warning("Failed event page %s: %s — skipping", event_path, e)
                    continue

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
                            client, url=page_url, event_url=event_url
                        )
                    except Exception as e:
                        logger.warning(
                            "  Results page %s for %s failed: %s", page, event_path, e
                        )
                        break

                    paths = parse_vehicle_paths_from_results(res_html)
                    new = 0
                    for p in paths:
                        if p not in seen_paths:
                            seen_paths.add(p)
                            all_paths.append(p)
                            new += 1
                    event_paths_count += len(paths)

                    logger.info(
                        "  Results page %s: %s on page (%s new), %s unique total",
                        page,
                        len(paths),
                        new,
                        len(all_paths),
                    )

                    if not paths or not next_results_page_url(res_html):
                        break
                    page += 1

                logger.info(
                    "  Event done: %s vehicle paths across %s result page(s)",
                    event_paths_count,
                    page,
                )

            logger.info("Collected %s unique vehicle paths — fetching details", len(all_paths))

            # --- Step 4: concurrent detail fetches ---------------------------
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
                    parsed = parse_vehicle_detail(h, vehicle_path=path)
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
                rows, fx, self.vehicle_filter, scraped_at
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info(
                "After min-price ($%s): %s -> %s records",
                f"{self.min_price:,}",
                before,
                len(records),
            )

        csv_path = self.output_dir / "broad_arrow_auction_results.csv"
        xlsx_path = self.output_dir / "broad_arrow_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Broad Arrow - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

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
