"""CarGurus — classifieds scraper orchestrator (used + new modes).

Discovery flow:
    POST /search?srpVariation=DEFAULT_SEARCH|NEW_CAR_SEARCH&sortType=PRICE&sortDirection=DESC&page=N
        → JSON ``{"tiles": [...]}`` with 22–23 LISTING_* rows per page plus
          a couple of merch/ad tiles we skip.
    GET  /details/<listingId>?_data=routes/($intl).details.$listingId
        → ``text/remix-deferred`` blob. First newline-terminated chunk is the
          main JSON with ``oldData.listing`` (description, colors, pictures,
          engine, transmission, VIN, etc.). Later chunks are deferred
          sub-resolvers (recommendations, ads) we don't need.

``--max-pages N`` = max SRP pages (24 listings each).
``--min-price USD`` = stop once the DESC-sorted price drops below.
``--mode used|new`` = picks srpVariation.
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
from shared.vehicle_filter import VehicleFilter

from . import record_builder
from .api_client import fetch_detail_payload, post_search_page
from .config import DEFAULT_PAGE_SIZE, MODE_NEW, MODE_USED
from .listing_parser import is_listing_tile, parse_detail_payload, parse_list_tile

logger = logging.getLogger(__name__)


class CarGurusScraper(BaseScraper):
    """Classifieds from [CarGurus](https://www.cargurus.com)."""

    SOURCE_NAME = "CarGurus"
    SOURCE_DOMAIN = "cargurus.com"
    DATABASE = "classifieds"

    def __init__(
        self,
        min_price: int = 0,
        output_dir="output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter: VehicleFilter | None = None,
        mode: str = MODE_USED,
    ):
        super().__init__(
            min_price=min_price,
            output_dir=output_dir,
            max_concurrent=max_concurrent,
            max_pages=max_pages,
            vehicle_filter=vehicle_filter,
        )
        if mode not in (MODE_USED, MODE_NEW):
            raise ValueError(f"CarGurus mode must be 'used' or 'new', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("CarGurus (%s) — Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited)", self.max_pages)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            # --- Step 1: paginate SRP until we run out / hit min-price ---
            flat_rows: list[dict] = []
            page = 0
            while True:
                page += 1
                try:
                    payload = await post_search_page(client, mode=self.mode, page=page)
                except Exception as e:
                    logger.error("SRP page %s failed: %s", page, e)
                    break

                tiles = payload.get("tiles") or []
                listings_on_page = [t for t in tiles if is_listing_tile(t)]
                if not listings_on_page:
                    # No listing tiles left — we've walked off the end.
                    logger.info("Page %s has no listing tiles — stopping", page)
                    break

                page_added = 0
                stop_low_price = False
                for tile in listings_on_page:
                    flat = parse_list_tile(tile)
                    if not flat:
                        continue
                    price = flat.get("asking_price")
                    if price is not None and price < self.min_price:
                        stop_low_price = True
                        break
                    if self.vehicle_filter and flat.get("listing_title"):
                        if not self.vehicle_filter.match(flat["listing_title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s: %s listings → %s kept (running total %s)",
                    page, len(listings_on_page), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) — stopping pagination", f"{self.min_price:,}",
                    )
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached — stopping", self.max_pages)
                    break

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.25))

            logger.info("Collected %s listings — fetching details", len(flat_rows))

            # --- Step 2: concurrent detail fetches ------------------------------
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
                        raw = await fetch_detail_payload(client, listing_id=flat["listing_id"])
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

            # --- Step 3: build records ----------------------------------------
            scraped_at = get_scraped_at()
            today_iso = datetime.now(tz=timezone.utc).date().isoformat()
            # Scope ``Source`` by mode so the mark-removed sweep only flips
            # rows for the exact mode we just scraped — used + new are
            # disjoint inventories and shouldn't Removed-flip each other.
            records: list[dict] = []
            for flat in flat_rows:
                rec = record_builder.build_record(
                    flat=flat,
                    detail=details.get(flat["record_id"]),
                    scraped_at=scraped_at,
                    today_iso=today_iso,
                    source="cargurus.com",
                    listing_segment=self.mode,
                )
                records.append(rec)

            logger.info("Built %s classifieds records", len(records))

        # CSV / XLSX for spot-checking. Filenames include the mode so used
        # + new runs don't overwrite each other.
        csv_path = self.output_dir / f"cargurus_{self.mode}_classifieds.csv"
        xlsx_path = self.output_dir / f"cargurus_{self.mode}_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("CarGurus (%s) — Complete: %s records", self.mode.upper(), len(records))
        logger.info("=" * 60)
        return records
