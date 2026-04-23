"""UsedCars.com — classifieds scraper orchestrator.

Discovery flow:
    GET apim.dealix.com/vis/v3/prod/get-inventory
        → paginate 29 listings per call, sorted ``highest_price`` DESC so
          ``--min-price`` short-circuits cleanly.
    GET apim.dealix.com/vis/v3/prod/get-vehicledetail
        → per-VIN detail payload, gives us ``sellerNotes`` (dealer prose)
          which isn't exposed on the list response.

``--max-pages N`` = max list pages (29 listings each).
``--min-price USD`` = stop once the DESC-sorted price drops below.

No ``--mode`` here: UsedCars.com's feed is used-only.
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
from .api_client import fetch_detail, fetch_list_page
from .config import DEFAULT_PAGE_SIZE, HARD_RESULT_CAP
from .listing_parser import _build_dealer_lookup, parse_detail_payload, parse_list_row

logger = logging.getLogger(__name__)


class UsedCarsComScraper(BaseScraper):
    """Classifieds from [UsedCars.com](https://www.usedcars.com)."""

    SOURCE_NAME = "UsedCars.com"
    SOURCE_DOMAIN = "usedcars.com"
    DATABASE = "classifieds"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("UsedCars.com - Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited)", self.max_pages)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            # --- Step 1: paginate inventory ----------------------------------
            flat_rows: list[dict] = []
            page = 0
            total_reported: int | None = None
            while True:
                page += 1
                try:
                    payload = await fetch_list_page(
                        client, page=page, page_size=DEFAULT_PAGE_SIZE,
                    )
                except Exception as e:
                    logger.error("Inventory page %s failed: %s", page, e)
                    break

                rows = payload.get("carsForSale") or []
                dealer_lookup = _build_dealer_lookup(payload.get("dealers"))
                if total_reported is None:
                    total_reported = payload.get("inventoryCount")
                    logger.info(
                        "Dealix reports %s total listings (capped at %s)",
                        total_reported, HARD_RESULT_CAP,
                    )

                if not rows:
                    break

                page_added = 0
                stop_low_price = False
                for row in rows:
                    flat = parse_list_row(row, dealer_lookup)
                    if not flat:
                        continue
                    price = flat.get("asking_price")
                    if price is not None and price < self.min_price:
                        stop_low_price = True
                        break
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s: %s rows -> %s kept (running total %s)",
                    page, len(rows), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) - stopping pagination",
                        f"{self.min_price:,}",
                    )
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached - stopping", self.max_pages)
                    break
                if page * DEFAULT_PAGE_SIZE >= HARD_RESULT_CAP:
                    logger.info("Reached 10k result cap - stopping")
                    break
                # Nothing left to paginate? Dealix returns a shrinking page
                # on the last page rather than an explicit ``hasNextPage``.
                if len(rows) < DEFAULT_PAGE_SIZE:
                    break

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.25))

            logger.info("Collected %s listings - fetching details", len(flat_rows))

            # --- Step 2: concurrent detail fetches ----------------------------
            detail_conc = max(1, min(self.max_concurrent, 6))
            sem = asyncio.Semaphore(detail_conc)
            details: dict[str, dict] = {}
            done = 0
            total = len(flat_rows)

            async def one(flat: dict) -> None:
                nonlocal done
                key = flat["record_id"]
                async with sem:
                    try:
                        raw = await fetch_detail(client, used_car_id=flat["used_car_id"])
                        parsed = parse_detail_payload(raw) if raw else {}
                    except Exception as e:
                        logger.debug("detail %s failed: %s", key, e)
                        parsed = {}
                    details[key] = parsed
                    done += 1
                    if done % 50 == 0 or done == total:
                        logger.info("Detail fetch %s/%s", done, total)
                await asyncio.sleep(
                    REQUEST_DELAY_SECONDS * 0.15 + random.uniform(0.03, 0.15),
                )

            await asyncio.gather(*(one(r) for r in flat_rows))

            # --- Step 3: build records ---------------------------------------
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
        csv_path = self.output_dir / "usedcars_com_classifieds.csv"
        xlsx_path = self.output_dir / "usedcars_com_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("UsedCars.com - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
