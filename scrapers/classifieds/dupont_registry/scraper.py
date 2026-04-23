"""duPont REGISTRY — classifieds scraper orchestrator.

Discovery flow:
    POST /api/graphql (``getCars``)   → paginate 24 listings per call, sorted
                                         by price DESC so ``--min-price`` short-
                                         circuits cleanly.
    GET  /car/{alias}/.../{car_id}    → HTML detail page. Parse JSON-LD ``Car``
                                         for colors + the long ``self.__next_f``
                                         push for the options / marketing text.

Record handling (differs from auction scrapers):
    shared.db.upsert_classified_record handles the lifecycle:
        inserted  — first time we see this URL
        updated   — price changed since the last scrape (writes Previous_asking_price)
        skipped   — same URL + price; only Last_seen_date + Scraped_at refresh.
    After the run, ``mark_removed_classifieds`` flips any still-Active rows not
    seen in this pass to ``Listing_status = 'Removed'``.

``--max-pages N`` = max GraphQL pages (24 listings each).
``--min-price USD`` = stop once sorted-desc price drops below.
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
from .api_client import fetch_detail_html, post_search_cars
from .config import DEFAULT_PAGE_SIZE
from .listing_parser import parse_detail_html, parse_list_row

logger = logging.getLogger(__name__)


class DupontRegistryScraper(BaseScraper):
    """Classifieds from [duPont REGISTRY](https://www.dupontregistry.com)."""

    SOURCE_NAME = "DuPont Registry"
    SOURCE_DOMAIN = "dupontregistry.com"
    DATABASE = "classifieds"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("DuPont Registry — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited)", self.max_pages)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            # --- Step 1: paginate getCars until we run out or hit min-price ---
            flat_rows: list[dict] = []
            offset = 0
            page = 0
            total_reported: int | None = None
            while True:
                page += 1
                try:
                    payload = await post_search_cars(
                        client, offset=offset, limit=DEFAULT_PAGE_SIZE,
                    )
                except Exception as e:
                    logger.error("getCars page %s failed: %s", page, e)
                    break

                rows = payload.get("data") or []
                pagination = payload.get("pagination") or {}
                if total_reported is None:
                    total_reported = pagination.get("total")
                    logger.info("GraphQL reports %s total listings", total_reported)

                if not rows:
                    break

                page_added = 0
                stop_low_price = False
                for row in rows:
                    price = row.get("price")
                    if price is not None and price < self.min_price:
                        stop_low_price = True
                        break
                    flat = parse_list_row(row)
                    if not flat:
                        continue
                    # Vehicle filter pre-detail to save per-listing fetches.
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s (offset %s): %s rows → %s kept (running total %s)",
                    page, offset, len(rows), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) — stopping pagination", f"{self.min_price:,}",
                    )
                    break
                if not pagination.get("hasMore"):
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached — stopping", self.max_pages)
                    break

                offset = int(pagination.get("nextCursor") or (offset + DEFAULT_PAGE_SIZE))
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
                        html = await fetch_detail_html(
                            client,
                            brand_alias=flat["brand_alias"],
                            model_alias=flat["model_alias"],
                            year=flat["year"],
                            vin=flat["vin"],
                            car_id=flat["car_id"],
                        )
                        parsed = parse_detail_html(html)
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

            # --- Step 3: build records + lifecycle upsert -----------------------
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
        csv_path = self.output_dir / "dupont_registry_classifieds.csv"
        xlsx_path = self.output_dir / "dupont_registry_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("DuPont Registry — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
