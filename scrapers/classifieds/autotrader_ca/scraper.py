"""AutoTrader.ca — classifieds scraper orchestrator (used + new modes).

Discovery flow:
    GET /cars/{ot_used|ot_new}?...   → HTML SRP, parsed once to grab the
                                       Next.js ``buildId`` that rotates on
                                       every deploy.
    GET /_next/data/<buildId>/legacyTrader/cars/<slug>.json?...
                                     → paginated JSON bodies with 20 listings
                                       each, sorted ``price DESC`` so
                                       ``--min-price`` short-circuits.

Descriptions (dealer prose) are in the list response — no detail fetch.
Prices are CAD; converted to USD via ``shared.fx_converter``.

``--mode used|new`` picks which SRP slug to use.
``--max-pages N`` = max list pages (20 per page; site caps at 200 pages).
``--min-price USD`` = compare against CAD prices (scraper does the check
after FX conversion so the floor is meaningful cross-currency).

Source tag is scoped per mode (``autotrader.ca/used`` vs ``autotrader.ca/new``)
so ``mark_removed_classifieds`` doesn't flip the other mode's rows to Removed.
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
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import CLASSIFIEDS_COLUMNS
from shared.vehicle_filter import VehicleFilter

from . import record_builder
from .api_client import _new_search_id, fetch_build_id, fetch_list_page, new_session
from .config import DEFAULT_PAGE_SIZE, HARD_PAGE_CAP, MODE_NEW, MODE_USED
from .listing_parser import parse_listing

logger = logging.getLogger(__name__)


class AutoTraderCaScraper(BaseScraper):
    """Classifieds from [AutoTrader.ca](https://www.autotrader.ca)."""

    SOURCE_NAME = "AutoTrader.ca"
    SOURCE_DOMAIN = "autotrader.ca"
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
            raise ValueError(f"AutoTrader.ca mode must be 'used' or 'new', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("AutoTrader.ca (%s) - Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited; site cap=%s)", self.max_pages, HARD_PAGE_CAP)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # curl_cffi session for AutoTrader.ca itself (Imperva-fronted).
        # FXConverter uses plain httpx to call Frankfurter, which is unrelated.
        async with new_session() as session, \
                httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
            # Step 0 — Discover buildId from the mode-matching HTML SRP.
            try:
                build_id = await fetch_build_id(session, self.mode)
            except Exception as e:
                logger.error("Failed to discover buildId: %s", e)
                return []

            search_id = _new_search_id()
            logger.info("Using search_id=%s", search_id)

            fx = FXConverter(fx_client)
            today_iso = datetime.now(tz=timezone.utc).date().isoformat()
            # AutoTrader.ca is CAD-native; convert using today's rate for the
            # whole run (per-listing date isn't exposed).
            fx_rate = await fx.get_rate(today_iso, "CAD")
            logger.info("CAD→USD rate: %s", fx_rate)

            # --- Step 1: paginate the JSON loader ----------------------------
            flat_rows: list[dict] = []
            page = 0
            total_reported: int | None = None
            while True:
                page += 1
                try:
                    props = await fetch_list_page(
                        session, mode=self.mode, build_id=build_id,
                        page=page, search_id=search_id, page_size=DEFAULT_PAGE_SIZE,
                    )
                except Exception as e:
                    logger.error("SRP page %s failed: %s", page, e)
                    break

                listings = props.get("listings") or []
                if total_reported is None:
                    total_reported = props.get("numberOfResults")
                    logger.info(
                        "AutoTrader.ca reports %s total listings (site cap = %s pages)",
                        total_reported, HARD_PAGE_CAP,
                    )

                if not listings:
                    break

                page_added = 0
                stop_low_price = False
                for listing in listings:
                    flat = parse_listing(listing)
                    if not flat:
                        continue
                    # CAD → USD for the price floor check so ``--min-price``
                    # is meaningful against the scraper's USD floor.
                    price_cad = flat.get("asking_price_cad")
                    if price_cad is not None and fx_rate and fx_rate == fx_rate:
                        price_usd = price_cad * fx_rate
                        if price_usd < self.min_price:
                            stop_low_price = True
                            break
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s: %s listings -> %s kept (running total %s)",
                    page, len(listings), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) - stopping pagination", f"{self.min_price:,}",
                    )
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached - stopping", self.max_pages)
                    break
                # Site's hard pagination cap.
                num_pages = props.get("numberOfPages") or HARD_PAGE_CAP
                if page >= min(num_pages, HARD_PAGE_CAP):
                    logger.info("Reached site pagination cap (%s pages) - stopping", num_pages)
                    break

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.25))

            logger.info("Collected %s listings", len(flat_rows))

            # --- Step 2: build records (no detail fetch needed) --------------
            scraped_at = get_scraped_at()
            records: list[dict] = []
            for flat in flat_rows:
                price_cad = flat.get("asking_price_cad")
                if flat.get("price_in_usd_override"):
                    # Dealer declared "PRICE IN USD" — skip CAD→USD conversion.
                    price_usd = float(price_cad) if price_cad is not None else None
                    rate = 1.0
                else:
                    price_usd, rate = await fx.convert_to_usd(price_cad, "CAD", today_iso)
                rec = record_builder.build_record(
                    flat=flat,
                    asking_price_usd=price_usd,
                    fx_rate=rate,
                    scraped_at=scraped_at,
                    today_iso=today_iso,
                    source="autotrader.ca",
                    listing_segment=self.mode,
                )
                records.append(rec)

            logger.info("Built %s classifieds records", len(records))

        # CSV / XLSX for spot-checking.
        csv_path = self.output_dir / f"autotrader_ca_{self.mode}_classifieds.csv"
        xlsx_path = self.output_dir / f"autotrader_ca_{self.mode}_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("AutoTrader.ca (%s) - Complete: %s records", self.mode.upper(), len(records))
        logger.info("=" * 60)
        return records
