"""The Market — auction results (JSON list + per-listing JSON)."""

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
from .api_client import TheMarketApiError
from .listing_parser import merge_summary, parse_list_row

logger = logging.getLogger(__name__)


class TheMarketScraper(BaseScraper):
    """Ended auctions from [The Market](https://www.themarket.co.uk/auctions/results)."""

    SOURCE_NAME = "The Market"
    SOURCE_DOMAIN = "themarket.co.uk"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("The Market — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max results API pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[dict] = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            await api_client.resolve_access_token(client)
            try:
                first = await api_client.fetch_results_page(client, page=1)
            except TheMarketApiError as e:
                logger.error("Results page 1 failed: %s", e)
                return []

            meta = first.get("meta") or {}
            total_pages = int(meta.get("total_pages") or 1)
            if self.max_pages > 0:
                total_pages = min(total_pages, self.max_pages)

            logger.info(
                "Results index: %s lots, %s per page, %s page(s) to fetch",
                meta.get("total"),
                meta.get("per_page"),
                total_pages,
            )

            for row in first.get("data") or []:
                if isinstance(row, dict):
                    summaries.append(parse_list_row(row))

            for page in range(2, total_pages + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))
                try:
                    payload = await api_client.fetch_results_page(client, page=page)
                except TheMarketApiError as e:
                    logger.warning("Results page %s: %s — stopping pagination", page, e)
                    break
                batch = payload.get("data") or []
                for row in batch:
                    if isinstance(row, dict):
                        summaries.append(parse_list_row(row))
                logger.info("Results page %s/%s — %s rows", page, total_pages, len(summaries))

        logger.info("Collected %s list rows; fetching listing JSON…", len(summaries))

        details: dict[str, dict] = {}
        sem = asyncio.Semaphore(self.max_concurrent)

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:

            async def one(row: dict) -> tuple[str, dict]:
                lid = row.get("record_id") or ""
                path = row.get("referer_path") or ""
                if not lid:
                    return "", {}
                async with sem:
                    try:
                        await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.35 + random.uniform(0, 0.25))
                        d = await api_client.fetch_listing_detail(client, lid, referer_path=path)
                        return lid, (d or {})
                    except Exception as e:
                        logger.warning("Detail %s: %s", lid, e)
                        return lid, {}

            tasks = [asyncio.create_task(one(r)) for r in summaries if r.get("record_id")]
            done = 0
            for coro in asyncio.as_completed(tasks):
                lid, d = await coro
                if lid:
                    details[lid] = d
                done += 1
                if done % 30 == 0 or done == len(tasks):
                    logger.info("Detail fetch %s/%s", done, len(tasks))

        merged = [merge_summary(dict(r), details.get(str(r.get("record_id") or ""), {})) for r in summaries]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
            fx = FXConverter(fx_client)
            scraped_at = get_scraped_at()
            all_records = await record_builder.build_records(
                merged, fx, self.vehicle_filter, scraped_at,
            )

        logger.info("Built %s records before min-price filter", len(all_records))
        records = self._filter_records_min_usd(all_records)
        logger.info("After min-price ($%s): %s records", f"{self.min_price:,}", len(records))

        csv_path = self.output_dir / "the_market_auction_results.csv"
        excel_path = self.output_dir / "the_market_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        logger.info("=" * 60)
        logger.info("The Market — Complete: %s records", len(records))
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
