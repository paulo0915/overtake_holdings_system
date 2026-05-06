"""Gooding & Company — discover lot paths from app.js bundle, fetch page-data JSON.

Discovery path:
    GET  /                                   — find current ``/app-*.js`` URL
    GET  /app-*.js                           — extract embedded ``[{path, matchPath}, …]`` array
         → filter to ``/lot/<slug>`` entries
    GET  /page-data/lot/<slug>/page-data.json — per-lot structured Contentful payload
         → Year/Make/Model/VIN/engine/estimates/salePrice/auction date/currency/description

--max-pages N = max lots to process (0 = all ~9k lots across every auction).
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
    fetch_app_js,
    fetch_home_html,
    fetch_lots_page_data,
    fetch_page_data,
    find_app_js_path,
)
from .listing_parser import (
    extract_paths_from_app_js,
    parse_lot_page_data,
    parse_lot_paths_from_lots_page,
    select_lot_paths,
)

logger = logging.getLogger(__name__)


class GoodingScraper(BaseScraper):
    """Past results from [Gooding & Company](https://www.goodingco.com)."""

    SOURCE_NAME = "Gooding & Company"
    SOURCE_DOMAIN = "goodingco.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Gooding & Company - Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_lots = self.max_pages  # 0 = unlimited
        if max_lots > 0:
            logger.info("Max lots (test mode): %s", max_lots)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # --- Step 1: discover lot paths via app.js + lots page-data -------
            home_html = await fetch_home_html(client)
            app_js = find_app_js_path(home_html)
            lot_paths: list[str] = []

            if app_js:
                logger.info("App bundle: %s", app_js)
                js_text = await fetch_app_js(client, app_js)
                entries = extract_paths_from_app_js(js_text)
                lot_paths = select_lot_paths(entries)
                logger.info("Discovered %s lot paths from app.js bundle", len(lot_paths))
            else:
                logger.warning("Could not find /app-*.js in homepage — skipping bundle discovery")

            # Secondary: /page-data/lots/page-data.json (Gatsby catalog index).
            # Merge any paths not already found via app.js.
            lots_payload = await fetch_lots_page_data(client)
            fallback_paths = parse_lot_paths_from_lots_page(lots_payload)
            if fallback_paths:
                existing = set(lot_paths)
                new_from_catalog = [p for p in fallback_paths if p not in existing]
                lot_paths.extend(new_from_catalog)
                logger.info(
                    "lots page-data: %s paths (%s new not in bundle)",
                    len(fallback_paths), len(new_from_catalog),
                )
            else:
                logger.debug("lots page-data returned no paths")

            if not lot_paths:
                logger.error("No lot paths discovered from any source — aborting")
                return []

            logger.info("Total unique lot paths to scrape: %s", len(lot_paths))

            if max_lots > 0:
                # Sample evenly across the full path list so test runs include a mix
                # of lot types rather than only the alphabetically-first entries
                # (which tend to be automobilia/scale-models, not vehicles).
                step = max(1, len(lot_paths) // max_lots)
                lot_paths = lot_paths[::step][:max_lots]
                logger.info("Limited to %s lot(s) for test run (sampled every %s)", max_lots, step)

            # --- Step 2: concurrent per-lot page-data fetches -----------------
            detail_conc = max(1, min(self.max_concurrent, 8))
            sem = asyncio.Semaphore(detail_conc)
            rows: list[dict] = []
            done = 0
            total = len(lot_paths)
            lock = asyncio.Lock()

            async def one(path: str) -> None:
                nonlocal done
                async with sem:
                    try:
                        payload = await fetch_page_data(client, path=path)
                        row = parse_lot_page_data(payload, path=path)
                    except Exception as e:
                        logger.debug("  lot %s failed: %s", path, e)
                        row = None
                    async with lock:
                        if row:
                            rows.append(row)
                        done += 1
                        if done % 100 == 0 or done == total:
                            logger.info("Lot fetch %s/%s (rows=%s)", done, total, len(rows))
                await asyncio.sleep(
                    REQUEST_DELAY_SECONDS * 0.08 + random.uniform(0.01, 0.1),
                )

            await asyncio.gather(*(one(p) for p in lot_paths))

            logger.info("Collected %s parsed lot rows — building records", len(rows))

            # --- Step 3: records + price filter + export ----------------------
            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at,
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info("After min-price ($%s): %s -> %s records",
                        f"{self.min_price:,}", before, len(records))

        csv_path = self.output_dir / "gooding_auction_results.csv"
        xlsx_path = self.output_dir / "gooding_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Gooding & Company - Complete: %s records", len(records))
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
