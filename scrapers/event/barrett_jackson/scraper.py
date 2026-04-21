"""Barrett-Jackson — previous docket results API (sold vehicle history)."""

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
from .api_client import discover_non_included_slugs, fetch_previous_docket_page
from .config import DEFAULT_PAGE_SIZE, DEFAULT_RESULT_TYPE, DEFAULT_SORT
from .listing_parser import parse_docket_entry

logger = logging.getLogger(__name__)


class BarrettJacksonScraper(BaseScraper):
    """Past auction results from [Barrett-Jackson](https://www.barrett-jackson.com)."""

    SOURCE_NAME = "Barrett-Jackson"
    SOURCE_DOMAIN = "barrett-jackson.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Barrett-Jackson - Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max API pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            non_included_slugs = await discover_non_included_slugs(client)

            try:
                first = await fetch_previous_docket_page(
                    client,
                    page=1,
                    page_size=DEFAULT_PAGE_SIZE,
                    sort=DEFAULT_SORT,
                    result_type=DEFAULT_RESULT_TYPE,
                    non_included_slugs=non_included_slugs,
                )
            except Exception as e:
                logger.error("API page 1 error: %s", e)
                return []

            meta = first.get("meta") or {}
            pag = meta.get("pagination") or {}
            try:
                page_count = int(pag.get("pageCount") or 1)
            except (TypeError, ValueError):
                page_count = 1
            total_hits = pag.get("total")

            max_page = page_count
            if self.max_pages > 0:
                max_page = min(page_count, self.max_pages)

            logger.info(
                "Docket API: total=%s, page_count=%s, fetching %s page(s), size=%s",
                total_hits,
                page_count,
                max_page,
                DEFAULT_PAGE_SIZE,
            )

            def _append_page(data: dict) -> None:
                for item in data.get("data") or []:
                    row = parse_docket_entry(item)
                    if row:
                        rows.append(row)

            _append_page(first)

            for page in range(2, max_page + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))
                try:
                    data = await fetch_previous_docket_page(
                        client,
                        page=page,
                        page_size=DEFAULT_PAGE_SIZE,
                        sort=DEFAULT_SORT,
                        result_type=DEFAULT_RESULT_TYPE,
                        non_included_slugs=NON_INCLUDED_SLUGS,
                    )
                except Exception as e:
                    logger.warning("API page %s: %s - stopping pagination", page, e)
                    break
                _append_page(data)
                if page % 10 == 0 or page == max_page:
                    logger.info("Fetched page %s/%s - %s lots", page, max_page, len(rows))

            logger.info("Collected %s lots; building records", len(rows))

            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at,
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info(
                "After min-price ($%s): %s -> %s records",
                f"{self.min_price:,}",
                before,
                len(records),
            )

        csv_path = self.output_dir / "barrett_jackson_auction_results.csv"
        xlsx_path = self.output_dir / "barrett_jackson_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Barrett-Jackson - Complete: %s records", len(records))
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
