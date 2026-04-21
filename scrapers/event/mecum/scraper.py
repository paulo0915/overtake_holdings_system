"""Mecum Auctions — Algolia sold index + per-lot HTML enrichment."""

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
from .api_client import MecumApiError, fetch_lot_html, fetch_sold_lots_page
from .listing_parser import parse_algolia_hit, parse_lot_html

logger = logging.getLogger(__name__)


class MecumScraper(BaseScraper):
    """Sold lots from [Mecum Auctions](https://www.mecum.com) via Algolia + lot pages."""

    SOURCE_NAME = "Mecum"
    SOURCE_DOMAIN = "mecum.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Mecum — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max Algolia pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[dict] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            try:
                first = await fetch_sold_lots_page(client, page=0)
            except MecumApiError as e:
                logger.error("Algolia page 0 failed: %s", e)
                return []
            except Exception as e:
                logger.error("Algolia page 0 error: %s", e)
                return []

            nb_pages = int(first.get("nbPages") or 1)
            if self.max_pages > 0:
                nb_pages = min(nb_pages, self.max_pages)

            logger.info(
                "Algolia sold lots: ~%s hits, %s page(s) to fetch",
                first.get("nbHits"),
                nb_pages,
            )

            def _hits_from_page(page_result: dict) -> list[dict]:
                hits = page_result.get("hits") or []
                return [h for h in hits if isinstance(h, dict)]

            for h in _hits_from_page(first):
                row = parse_algolia_hit(h)
                if row:
                    summaries.append(row)

            for page in range(1, nb_pages):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))
                try:
                    data = await fetch_sold_lots_page(client, page=page)
                except Exception as e:
                    logger.warning("Algolia page %s: %s — stopping pagination", page, e)
                    break
                for h in _hits_from_page(data):
                    row = parse_algolia_hit(h)
                    if row:
                        summaries.append(row)
                if (page + 1) % 10 == 0:
                    logger.info("Algolia page %s/%s — %s lots", page + 1, nb_pages, len(summaries))

            logger.info("Collected %s lot rows; fetching detail HTML", len(summaries))

            detail_conc = max(1, min(self.max_concurrent, 4))
            sem = asyncio.Semaphore(detail_conc)
            lock = asyncio.Lock()
            details: dict[str, dict | None] = {}
            done = 0
            total = len(summaries)

            async def one(row: dict) -> None:
                nonlocal done
                wid = str(row.get("web_id") or "")
                path = row.get("permalink") or ""
                async with sem:
                    html = await fetch_lot_html(client, path=path)
                    parsed = parse_lot_html(html)
                    async with lock:
                        details[wid] = parsed
                        done += 1
                        if done % 50 == 0 or done == total:
                            logger.info("Detail fetch %s/%s", done, total)
                # Sleep outside the semaphore so the slot frees immediately for the next task.
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.35 + random.uniform(0.05, 0.25))

            await asyncio.gather(*(one(s) for s in summaries))

            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                summaries, details, fx, self.vehicle_filter, scraped_at,
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info("After min-price ($%s): %s -> %s records", f"{self.min_price:,}", before, len(records))

        csv_path = self.output_dir / "mecum_auction_results.csv"
        xlsx_path = self.output_dir / "mecum_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Mecum — Complete: %s records", len(records))
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
