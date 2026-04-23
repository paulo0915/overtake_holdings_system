"""Cars.com — classifieds scraper orchestrator.

Discovery flow:
    POST graph.cars.com/graphql/api (``SearchResultsPageSearch``)
        → paginate 22 listings per page, sorted ``LIST_PRICE_DESC`` so
          ``--min-price`` short-circuits cleanly.
    POST graph.cars.com/graphql/api (``listingDetails``)
        → one call per row, returns the full VDP payload including the
          seller-written ``adDescription`` HTML.

The HTML VDP page (``/vehicledetail/<listingId>/``) is Akamai-protected
and blocks both httpx and curl_cffi Chrome impersonation — going through
GraphQL avoids it entirely.

``--max-pages N`` = max GraphQL search pages (22 listings each).
``--min-price USD`` = stop once the DESC-sorted price drops below.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import httpx

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.normalizer import get_scraped_at
from shared.schemas import CLASSIFIEDS_COLUMNS

from . import record_builder
from .api_client import post_listing_details, post_search_list
from .config import DEFAULT_PAGE_SIZE, HARD_RESULT_CAP
from .listing_parser import parse_detail_payload, parse_list_row

logger = logging.getLogger(__name__)


class CarsComScraper(BaseScraper):
    """Classifieds from [Cars.com](https://www.cars.com)."""

    SOURCE_NAME = "Cars.com"
    SOURCE_DOMAIN = "cars.com"
    DATABASE = "classifieds"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Cars.com — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited)", self.max_pages)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            # --- Step 1: paginate search until min-price / max-pages / cap ---
            flat_rows: list[dict] = []
            page = 0
            total_reported: int | None = None
            while True:
                page += 1
                try:
                    payload = await post_search_list(
                        client, page=page, page_size=DEFAULT_PAGE_SIZE,
                    )
                except Exception as e:
                    logger.error("Search page %s failed: %s", page, e)
                    break

                results = payload.get("results") or []
                if total_reported is None:
                    total_reported = (payload.get("metadata") or {}).get("totalListings")
                    logger.info(
                        "GraphQL reports %s total listings (capped at %s)",
                        total_reported, HARD_RESULT_CAP,
                    )

                if not results:
                    break

                page_added = 0
                stop_low_price = False
                for row in results:
                    # ``results`` is a union — skip ads / editorial cards.
                    if row.get("__typename") != "SrpListingGridCard":
                        continue
                    flat = parse_list_row(row)
                    if not flat:
                        continue
                    price = flat.get("asking_price")
                    if price is not None and price < self.min_price:
                        stop_low_price = True
                        break
                    # Vehicle filter pre-detail to skip unwanted detail fetches.
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s: %s rows → %s kept (running total %s)",
                    page, len(results), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) — stopping pagination", f"{self.min_price:,}",
                    )
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached — stopping", self.max_pages)
                    break
                # Cars.com hard-caps pagination around 10k results regardless
                # of ``totalNumberOfRecords``.
                if page * DEFAULT_PAGE_SIZE >= HARD_RESULT_CAP:
                    logger.info("Reached Cars.com 10k result cap — stopping")
                    break

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.25))

            logger.info("Collected %s listings — fetching details", len(flat_rows))

            # --- Step 2: concurrent detail fetches ------------------------------
            detail_conc = max(1, min(self.max_concurrent, 6))
            sem = asyncio.Semaphore(detail_conc)
            details: dict[str, dict | None] = {}
            done = 0
            total = len(flat_rows)

            async def one(flat: dict) -> None:
                nonlocal done
                key = flat["record_id"]
                async with sem:
                    try:
                        raw = await post_listing_details(
                            client, listing_id=flat["listing_id"],
                        )
                        parsed = parse_detail_payload(raw)
                    except Exception as e:
                        logger.debug("detail %s failed: %s", key, e)
                        parsed = None
                    details[key] = parsed
                    done += 1
                    if done % 50 == 0 or done == total:
                        logger.info("Detail fetch %s/%s", done, total)
                await asyncio.sleep(
                    REQUEST_DELAY_SECONDS * 0.15 + random.uniform(0.03, 0.15),
                )

            await asyncio.gather(*(one(r) for r in flat_rows))

            # --- Step 3: build records ----------------------------------------
            scraped_at = get_scraped_at()
            today_iso = datetime.now(tz=timezone.utc).date().isoformat()
            records: list[dict] = []
            for flat in flat_rows:
                rec = record_builder.build_record(
                    flat=flat,
                    detail=details.get(flat["record_id"]),
                    scraped_at=scraped_at,
                    today_iso=today_iso,
                )
                records.append(rec)

            logger.info("Built %s classifieds records", len(records))

        # CSV / XLSX for spot-checking.
        csv_path = self.output_dir / "cars_com_classifieds.csv"
        xlsx_path = self.output_dir / "cars_com_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Cars.com — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
