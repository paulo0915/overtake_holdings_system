"""Carbuckle — sold auction results via Typesense search API."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timezone

import httpx

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from . import api_client, record_builder
from .api_client import CarbuckleApiError, fetch_listing_html
from .config import PER_PAGE
from .listing_parser import merge_firestore_into_row, parse_document, parse_vin_from_listing_html

logger = logging.getLogger(__name__)


def _selling_date_is_future(selling_date: str | None) -> bool:
    """True if ``YYYY-MM-DD`` is strictly after today (UTC).

    A sold listing should not have a sale date in the future; that usually means
    we fell back to ``dateEnd`` (scheduled end) and should re-resolve from Firestore.
    """
    if not selling_date or not str(selling_date).strip():
        return False
    try:
        d = datetime.strptime(str(selling_date).strip(), "%Y-%m-%d").date()
        return d > datetime.now(timezone.utc).date()
    except ValueError:
        return False


class CarbuckleScraper(BaseScraper):
    """Sold auctions from [Carbuckle](https://www.carbuckle.com)."""

    SOURCE_NAME = "Carbuckle"
    SOURCE_DOMAIN = "carbuckle.com"
    DATABASE = "auctions"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Carbuckle — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max API pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []

        async with httpx.AsyncClient(timeout=30) as client:
            # Page 1 to discover total count
            try:
                first = await api_client.fetch_page(client, 1)
            except CarbuckleApiError as e:
                logger.error("Failed to fetch page 1: %s", e)
                return []

            found = first.get("found", 0)
            total_pages = math.ceil(found / PER_PAGE) if found else 1
            if self.max_pages > 0:
                total_pages = min(total_pages, self.max_pages)

            logger.info("Total sold listings: %s | Pages to fetch: %s", found, total_pages)

            rows.extend(self._parse_hits(first.get("hits", [])))

            for page in range(2, total_pages + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.4))
                try:
                    data = await api_client.fetch_page(client, page)
                    rows.extend(self._parse_hits(data.get("hits", [])))
                    logger.info("Page %s/%s — %s rows so far", page, total_pages, len(rows))
                except CarbuckleApiError as e:
                    logger.warning("Page %s error: %s — stopping early", page, e)
                    break

            logger.info("Parsed %s total listings", len(rows))

            # Enrich rows that have missing descriptions/specs via Firestore
            await self._enrich_from_firestore(client, rows)

            fx = FXConverter(client)
            filtered = await self._filter_by_min_price(rows, fx)
            logger.info(
                "After min-price filter ($%s): %s listings",
                f"{self.min_price:,}",
                len(filtered),
            )

            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                filtered, fx, self.vehicle_filter, scraped_at,
            )

        logger.info("Built %s records", len(records))

        csv_path = self.output_dir / "carbuckle_auction_results.csv"
        excel_path = self.output_dir / "carbuckle_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        logger.info("=" * 60)
        logger.info("Carbuckle — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    def _parse_hits(self, hits: list[dict]) -> list[dict]:
        out = []
        for h in hits:
            doc = h.get("document", {})
            if not doc:
                continue
            try:
                out.append(parse_document(doc))
            except Exception as e:
                logger.warning("Failed to parse doc %s: %s", doc.get("id"), e)
        return out

    async def _enrich_from_firestore(
        self, client: httpx.AsyncClient, rows: list[dict]
    ) -> None:
        """Fetch Firestore docs for listings missing engine/description and merge data."""
        needs_enrich = [
            r for r in rows
            if not r.get("engine_spec")
            or not r.get("transmission")
            or not r.get("listing_description")
            or len(r.get("listing_description", "")) < 120
            or r.get("amount") is None       # CLASSIFIED price lives in priceBuy
            or not r.get("country")
            or _selling_date_is_future(r.get("selling_date"))
            or not r.get("vin")
        ]
        if not needs_enrich:
            return

        logger.info(
            "Fetching Firestore details for %s/%s listings with missing specs",
            len(needs_enrich), len(rows),
        )
        enriched = 0
        for r in needs_enrich:
            lid = r.get("record_id") or ""
            if not lid:
                continue
            fs_doc = await api_client.fetch_firestore_doc(client, lid)
            if fs_doc:
                merge_firestore_into_row(r, fs_doc)
                enriched += 1
            await asyncio.sleep(0.15)

        logger.info("Firestore enrichment complete: %s docs fetched", enriched)

        # VIN fallback: fetch listing HTML for rows still missing VIN
        missing_vin = [r for r in rows if not r.get("vin") and r.get("listing_url")]
        if missing_vin:
            logger.info("Fetching listing HTML for %s rows missing VIN", len(missing_vin))
            vin_found = 0
            for r in missing_vin:
                path = (r.get("listing_url") or "").split("/listing/")[-1]
                if not path:
                    continue
                html = await fetch_listing_html(client, path)
                v = parse_vin_from_listing_html(html)
                if v:
                    r["vin"] = v
                    vin_found += 1
                await asyncio.sleep(0.3)
            logger.info("VIN HTML fallback: found %s/%s", vin_found, len(missing_vin))

    async def _filter_by_min_price(
        self, rows: list[dict], fx: FXConverter
    ) -> list[dict]:
        if self.min_price <= 0:
            return list(rows)

        out: list[dict] = []
        for r in rows:
            amt = r.get("amount")
            if amt is None:
                continue
            cur = r.get("currency") or "CAD"
            date_str = r.get("selling_date")
            usd, _ = await fx.convert_to_usd(amt, cur, date_str)
            if usd is not None and usd >= self.min_price:
                out.append(r)
        return out
