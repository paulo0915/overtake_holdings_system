"""Collecting Cars — Typesense sold cars + per-listing HTML (Cloudflare-safe GET)."""

from __future__ import annotations

import asyncio
import logging
import math
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
from .api_client import CollectingCarsApiError
from .config import MAX_DETAILS, PER_PAGE, RESULTS_REFERER
from .listing_parser import merge_summary, parse_detail_html, parse_typesense_document

logger = logging.getLogger(__name__)


class CollectingCarsScraper(BaseScraper):
    """Sold car auctions from [Collecting Cars](https://collectingcars.com)."""

    SOURCE_NAME = "Collecting Cars"
    SOURCE_DOMAIN = "collectingcars.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Collecting Cars — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max Typesense pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[dict] = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as ts_client:
            try:
                first = await api_client.fetch_typesense_page(
                    ts_client, page=1, per_page=PER_PAGE,
                )
            except CollectingCarsApiError as e:
                logger.error("Typesense page 1 failed: %s", e)
                return []

            found = int(first.get("found") or 0)
            total_pages = math.ceil(found / PER_PAGE) if found else 1
            if self.max_pages > 0:
                total_pages = min(total_pages, self.max_pages)

            logger.info("Sold cars (lotType=car): %s | Pages: %s", found, total_pages)

            def _hits(payload: dict) -> list[dict]:
                return [h.get("document") or {} for h in (payload.get("hits") or [])]

            for doc in _hits(first):
                parsed = parse_typesense_document(doc)
                if parsed is not None:
                    summaries.append(parsed)

            for page in range(2, total_pages + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.4))
                try:
                    payload = await api_client.fetch_typesense_page(
                        ts_client, page=page, per_page=PER_PAGE,
                    )
                except CollectingCarsApiError as e:
                    logger.warning("Typesense page %s: %s — stopping", page, e)
                    break
                for doc in _hits(payload):
                    parsed = parse_typesense_document(doc)
                    if parsed is not None:
                        summaries.append(parsed)
                logger.info("Typesense page %s/%s — %s rows", page, total_pages, len(summaries))

        if MAX_DETAILS > 0:
            summaries = summaries[:MAX_DETAILS]
            logger.info("COLLECTING_CARS_MAX_DETAILS=%s — capping listing fetches", MAX_DETAILS)

        # Pre-filter before the expensive per-listing HTML fetch. Typesense already
        # carries price and title, so we can discard listings that clearly won't pass
        # the min-price floor or vehicle filter without making a single extra request.
        _pre_len = len(summaries)
        if self.min_price > 0:
            # 70% floor gives ~30% FX headroom (GBP/EUR vs USD).
            _price_floor = int(self.min_price * 0.70)
            summaries = [
                r for r in summaries
                if r.get("amount_native") is None or (r.get("amount_native") or 0) >= _price_floor
            ]
        if self.vehicle_filter:
            summaries = [
                r for r in summaries
                if self.vehicle_filter.match(r.get("title") or "", "")
            ]
        if len(summaries) < _pre_len:
            logger.info("Pre-filter: %s → %s rows before HTML fetch", _pre_len, len(summaries))

        # Hard cap: never HTML-fetch more than 150 listings regardless of min_price
        # setting. Typesense returns price-desc order, so the cap keeps the highest-value
        # listings and prevents runaway fetches when the price floor is low/zero.
        _HTML_CAP = 150
        if len(summaries) > _HTML_CAP:
            summaries = summaries[:_HTML_CAP]
            logger.info("HTML fetch capped at %d rows", _HTML_CAP)

        logger.info("Collected %s Typesense rows; fetching listing HTML…", len(summaries))

        details: dict[str, dict] = {}
        # Parallel listing GETs often trip Cloudflare; keep detail fetches to 1–2 at a time.
        detail_concurrency = max(1, min(2, self.max_concurrent))
        logger.info("Listing HTML concurrency: %s (capped for Cloudflare)", detail_concurrency)

        async with AsyncSession(impersonate="chrome124") as session:
            await session.get(RESULTS_REFERER, timeout=float(REQUEST_TIMEOUT))

            sem = asyncio.Semaphore(detail_concurrency)

            async def one(row: dict) -> tuple[str, dict]:
                slug = row.get("slug") or ""
                if not slug:
                    return "", {}
                async with sem:
                    try:
                        await asyncio.sleep(0.2 + random.uniform(0, 0.5))
                        html = await api_client.fetch_listing_html(session, slug)
                        d = parse_detail_html(html)
                        await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.05, 0.3))
                        return slug, d
                    except Exception as e:
                        logger.warning("Detail %s: %s", slug, e)
                        return slug, {}

            tasks = [asyncio.create_task(one(r)) for r in summaries if r.get("slug")]
            done = 0
            for coro in asyncio.as_completed(tasks):
                slug, d = await coro
                if slug:
                    details[slug] = d
                done += 1
                if done % 25 == 0 or done == len(tasks):
                    logger.info("Detail fetch %s/%s", done, len(tasks))

        merged = [merge_summary(dict(r), details.get(r.get("slug") or "", {})) for r in summaries]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
            fx = FXConverter(fx_client)
            scraped_at = get_scraped_at()
            all_records = await record_builder.build_records(
                merged, fx, self.vehicle_filter, scraped_at,
            )

        logger.info("Built %s records before min-price filter", len(all_records))
        records = self._filter_records_min_usd(all_records)
        logger.info("After min-price ($%s): %s records", f"{self.min_price:,}", len(records))

        csv_path = self.output_dir / "collecting_cars_auction_results.csv"
        excel_path = self.output_dir / "collecting_cars_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        logger.info("=" * 60)
        logger.info("Collecting Cars — Complete: %s records", len(records))
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
            if int(usd) >= self.min_price:
                out.append(r)
        return out
