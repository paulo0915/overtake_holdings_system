"""Worldwide Auctioneers — POST ``/car-search/`` all-lots response parser.

Flow
----
1. ``curl_cffi`` session impersonates Chrome (avoids empty/500 from plain httpx).
2. Warmup: ``GET /`` (main) → ``GET /results/`` (listings sub-domain).
3. ``POST /car-search/`` with ``auctionid=0 / make=All`` → ~5 MB HTML containing
   every historical lot inline (no pagination, no per-product fetches required).
4. Parse each ``div.col.span_12[style*=margin]`` block:
   - Lot number, title (→ year / make / model), auction event name, sold status,
     price (USD), image URL.
   - Lots whose title does not start with a 4-digit year are dropped (memorabilia,
     signs, prints, etc.).
   - Lots with "Undisclosed" price are marked Sold with no price value.
5. Export to CSV + Excel.

``--max-pages N``: cap exported lots to N (0 = all).  For a quick smoke test
use ``--max-pages 50``.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from curl_cffi.requests import AsyncSession

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from . import record_builder
from .api_client import fetch_product_html, post_car_search, warmup, fetch_results_html
from .listing_parser import car_search_post_body, parse_car_search_results, parse_product_page

logger = logging.getLogger(__name__)


class WorldwideAuctioneersScraper(BaseScraper):
    """All historical lots from Worldwide Auctioneers via POST ``/car-search/``.

    ``max_pages`` = maximum lots to export (0 = export everything).
    """

    SOURCE_NAME = "Worldwide Auctioneers"
    SOURCE_DOMAIN = "listings.worldwideauctioneers.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Worldwide Auctioneers — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info(
                "Lot cap (test mode): %s — use --max-pages 0 for all lots", self.max_pages
            )
        else:
            logger.info("Lot cap: none — exporting all lots")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with AsyncSession(impersonate="chrome124") as session:
            await warmup(session)
            logger.info("Session warmed")

            # needed for session cookies / Referer
            await fetch_results_html(session)

            logger.info("POST /car-search/ (all auctions, all makes) — may take ~20s …")
            form = car_search_post_body()
            html = await post_car_search(session, form)
            logger.info("POST response: %s bytes", len(html))

            rows = parse_car_search_results(html)
            car_rows = len(rows)
            logger.info(
                "Parsed %s car lots (non-car lots filtered out — memorabilia, signs, etc.)",
                car_rows,
            )

            if self.max_pages > 0:
                rows = rows[: self.max_pages]
                logger.info("Capped to %s lots for test run", len(rows))

            # Pre-filter by min-price BEFORE any per-lot fetching. The search
            # response carries the hammer ``price`` per row for sold lots, so
            # we can drop sub-floor rows up-front. Unsold/no-price-disclosed
            # rows are kept (the post-filter at the end handles them).
            if self.min_price > 0:
                before = len(rows)
                rows = [
                    r for r in rows
                    if (
                        not r.get("sold")  # keep unsold/no-price rows for post-filter
                        or r.get("price") is None
                        or (isinstance(r.get("price"), int) and r["price"] >= self.min_price)
                    )
                ]
                logger.info(
                    "Pre-filter min-price ($%s): %s -> %s lots",
                    f"{self.min_price:,}", before, len(rows),
                )

            # Fetch detail pages only for rows whose product_url was already
            # populated by the search response. We do NOT slug-match against
            # the /inventory/ grid: it only ever exposes ~30 currently-active
            # listings (~5/100 hit rate on past lots last measured), so the
            # round-trip cost wasn't justified by the data it returned.
            rows_with_url = [r for r in rows if r.get("product_url")]
            if rows_with_url:
                logger.info(
                    "Fetching product pages for %s/%s lots with known URLs…",
                    len(rows_with_url), len(rows),
                )
                detail_sem = asyncio.Semaphore(max(1, min(self.max_concurrent, 4)))
                done_count = 0

                async def _fetch_product(row: dict) -> None:
                    nonlocal done_count
                    async with detail_sem:
                        product_html = await fetch_product_html(session, row["product_url"])
                        detail = parse_product_page(product_html)
                        for key, val in detail.items():
                            if val is not None:
                                row[key] = val
                        done_count += 1
                        if done_count % 50 == 0 or done_count == len(rows_with_url):
                            logger.info(
                                "Product page fetch %s/%s",
                                done_count, len(rows_with_url),
                            )

                await asyncio.gather(*(_fetch_product(r) for r in rows_with_url))
            else:
                logger.info(
                    "No product URLs in search response - detail fields (VIN, "
                    "engine, etc.) will be empty for all rows. This is expected "
                    "for past Worldwide auctions; only the search summary is "
                    "available for delisted lots."
                )

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
            fx = FXConverter(fx_client)
            scraped_at = get_scraped_at()
            all_records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at,
            )

        with_price = sum(1 for r in all_records if r.get("Selling_price"))
        no_price = len(all_records) - with_price
        logger.info(
            "Records: %s total | %s with price | %s price undisclosed/missing",
            len(all_records),
            with_price,
            no_price,
        )

        records = self._filter_records_min_usd(all_records)
        logger.info(
            "After min-price ($%s): %s -> %s records",
            f"{self.min_price:,}",
            len(all_records),
            len(records),
        )

        csv_path = self.output_dir / "worldwide_auctioneers_results.csv"
        xlsx_path = self.output_dir / "worldwide_auctioneers_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Worldwide Auctioneers — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    def _filter_records_min_usd(self, rows: list[dict]) -> list[dict]:
        if self.min_price <= 0:
            return list(rows)
        out: list[dict] = []
        for r in rows:
            usd = r.get("Selling_price_USD")
            if usd is None:
                continue
            try:
                if int(usd) >= self.min_price:
                    out.append(r)
            except (TypeError, ValueError):
                continue
        return out
