"""Car & Classic — sold auction results (Inertia JSON + per-listing partial / HTML fallback)."""

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

from . import api_client, record_builder
from .api_client import CarAndClassicApiError
from .listing_parser import merge_listing_partial, parse_search_row

logger = logging.getLogger(__name__)


class CarAndClassicScraper(BaseScraper):
    """Ended auctions from [Car & Classic](https://www.carandclassic.com/auctions/results)."""

    SOURCE_NAME = "Car & Classic"
    SOURCE_DOMAIN = "carandclassic.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Car & Classic — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max results pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[dict] = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                await api_client.bootstrap_inertia(client)
            except CarAndClassicApiError as e:
                logger.error("Bootstrap failed: %s", e)
                return []

            try:
                first = await api_client.fetch_results_page(client, page=1)
            except CarAndClassicApiError as e:
                logger.error("Results page 1 failed: %s", e)
                return []

            sr = first.get("props", {}).get("searchResults") or {}
            pag = sr.get("pagination") or {}
            total_pages = int(pag.get("last_page") or 1)
            if self.max_pages > 0:
                total_pages = min(total_pages, self.max_pages)

            logger.info(
                "Auction results index: ~%s lots, %s page(s) to fetch",
                pag.get("total"),
                total_pages,
            )

            for row in sr.get("data") or []:
                if isinstance(row, dict):
                    parsed = parse_search_row(row)
                    if parsed is not None:
                        summaries.append(parsed)

            for page in range(2, total_pages + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))
                try:
                    payload = await api_client.fetch_results_page(client, page=page)
                except CarAndClassicApiError as e:
                    logger.warning("Results page %s: %s — stopping pagination", page, e)
                    break
                batch = (payload.get("props") or {}).get("searchResults", {}).get("data") or []
                for row in batch:
                    if isinstance(row, dict):
                        parsed = parse_search_row(row)
                        if parsed is not None:
                            summaries.append(parsed)
                logger.info("Results page %s/%s — %s rows", page, total_pages, len(summaries))

            logger.info("Collected %s list rows; fetching listing details…", len(summaries))

            details: dict[str, dict] = {}
            detail_concurrency = max(1, min(2, self.max_concurrent))
            logger.info("Listing fetch concurrency: %s", detail_concurrency)
            sem = asyncio.Semaphore(detail_concurrency)

            async def one(row: dict) -> tuple[str, dict | None]:
                rid = str(row.get("record_id") or "")
                path = (row.get("auction_path") or "").strip()
                if not rid or not path:
                    return "", None
                async with sem:
                    try:
                        await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.1, 0.35))
                        return rid, await api_client.fetch_listing_partial(client, auction_path=path)
                    except Exception as e:
                        logger.warning("Detail %s: %s", path, e)
                        return rid, None

            tasks = [asyncio.create_task(one(r)) for r in summaries if r.get("record_id")]
            done = 0
            for coro in asyncio.as_completed(tasks):
                rid, d = await coro
                if rid:
                    details[rid] = d
                done += 1
                if done % 25 == 0 or done == len(tasks):
                    logger.info("Detail fetch %s/%s", done, len(tasks))

        merged = [
            merge_listing_partial(dict(r), details.get(str(r.get("record_id") or "")))
            for r in summaries
        ]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
            fx = FXConverter(fx_client)
            scraped_at = get_scraped_at()
            all_records = await record_builder.build_records(
                merged, fx, self.vehicle_filter, scraped_at,
            )

        logger.info("Built %s records before min-price filter", len(all_records))
        records = self._filter_records_min_usd(all_records)
        logger.info("After min-price ($%s): %s records", f"{self.min_price:,}", len(records))

        csv_path = self.output_dir / "car_and_classic_auction_results.csv"
        excel_path = self.output_dir / "car_and_classic_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        logger.info("=" * 60)
        logger.info("Car & Classic — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    def _filter_records_min_usd(self, rows: list[dict]) -> list[dict]:
        if self.min_price <= 0:
            return list(rows)
        out: list[dict] = []
        for r in rows:
            usd = r.get("Selling_price_USD")
            if usd is None:
                usd = r.get("Bid_price_USD")
            if usd is None:
                continue
            if int(usd) >= self.min_price:
                out.append(r)
        return out
