"""Gooding & Company — discover lot paths from app.js bundle, fetch page-data JSON.

Discovery path:
    GET  /                                   — find current ``/app-*.js`` URL
    GET  /app-*.js                           — extract embedded ``[{path, matchPath}, …]`` array
         → filter to ``/lot/<slug>`` entries
    GET  /page-data/lot/<slug>/page-data.json — per-lot structured Contentful payload
         → Year/Make/Model/VIN/engine/estimates/salePrice/auction date/currency/description

--max-pages N = max lots to process (0 = all ~9k lots across every auction).
"""

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
from .api_client import (
    fetch_app_js,
    fetch_auction_html,
    fetch_home_html,
    fetch_lots_page_data,
    fetch_page_data,
    find_app_js_path,
    iter_upcoming_vehicles_graphql,
    parse_early_consignments_filters,
    parse_navbar_auction_paths,
)
from .listing_parser import (
    extract_paths_from_app_js,
    parse_lot_page_data,
    parse_lot_paths_from_lots_page,
    select_lot_paths,
)

logger = logging.getLogger(__name__)


class GoodingScraper(BaseScraper):
    """Gooding & Company scraper.

    ``mode='past'`` (default) — iterate every lot in the app.js bundle and
    keep the sold ones with a hammer price.
    ``mode='upcoming'`` — discover auctions from the homepage navbar, keep
    only those with a 'View Early Consignments' button. For each, replay the
    site's ``GetVehiclesAndFilters`` GraphQL persisted query to discover
    early-consignment lot slugs (these lots are NOT in the app.js bundle
    yet), then fetch each lot's page-data the same way past mode does.
    """

    SOURCE_NAME = "Gooding & Company"
    SOURCE_DOMAIN = "goodingco.com"
    DATABASE = "auctions"

    def __init__(self, *args, mode: str = "past", **kwargs):
        super().__init__(*args, **kwargs)
        if mode not in ("past", "upcoming"):
            raise ValueError(f"Gooding mode must be 'past' or 'upcoming', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Gooding & Company (%s) - Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_lots = self.max_pages  # 0 = unlimited
        if max_lots > 0:
            logger.info("Max lots (test mode): %s", max_lots)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        if self.mode == "upcoming":
            return await self._run_upcoming()

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # --- Step 1: discover lot paths via app.js + lots page-data -------
            home_html = await fetch_home_html(client)
            app_js = find_app_js_path(home_html)
            lot_paths: list[str] = []

            if app_js:
                logger.info("App bundle: %s", app_js)
                js_text = await fetch_app_js(client, app_js)
                entries = extract_paths_from_app_js(js_text)
                lot_paths = select_lot_paths(entries)
                logger.info("Discovered %s lot paths from app.js bundle", len(lot_paths))
            else:
                logger.warning("Could not find /app-*.js in homepage — skipping bundle discovery")

            # Secondary: /page-data/lots/page-data.json (Gatsby catalog index).
            # Merge any paths not already found via app.js.
            lots_payload = await fetch_lots_page_data(client)
            fallback_paths = parse_lot_paths_from_lots_page(lots_payload)
            if fallback_paths:
                existing = set(lot_paths)
                new_from_catalog = [p for p in fallback_paths if p not in existing]
                lot_paths.extend(new_from_catalog)
                logger.info(
                    "lots page-data: %s paths (%s new not in bundle)",
                    len(fallback_paths), len(new_from_catalog),
                )
            else:
                logger.debug("lots page-data returned no paths")

            if not lot_paths:
                logger.error("No lot paths discovered from any source — aborting")
                return []

            logger.info("Total unique lot paths to scrape: %s", len(lot_paths))

            if max_lots > 0:
                # Sample evenly across the full path list so test runs include a mix
                # of lot types rather than only the alphabetically-first entries
                # (which tend to be automobilia/scale-models, not vehicles).
                step = max(1, len(lot_paths) // max_lots)
                lot_paths = lot_paths[::step][:max_lots]
                logger.info("Limited to %s lot(s) for test run (sampled every %s)", max_lots, step)

            # --- Step 2: concurrent per-lot page-data fetches -----------------
            detail_conc = max(1, min(self.max_concurrent, 8))
            sem = asyncio.Semaphore(detail_conc)
            rows: list[dict] = []
            done = 0
            total = len(lot_paths)
            lock = asyncio.Lock()

            async def one(path: str) -> None:
                nonlocal done
                async with sem:
                    try:
                        payload = await fetch_page_data(client, path=path)
                        row = parse_lot_page_data(payload, path=path)
                    except Exception as e:
                        logger.debug("  lot %s failed: %s", path, e)
                        row = None
                    async with lock:
                        if row:
                            rows.append(row)
                        done += 1
                        if done % 100 == 0 or done == total:
                            logger.info("Lot fetch %s/%s (rows=%s)", done, total, len(rows))
                await asyncio.sleep(
                    REQUEST_DELAY_SECONDS * 0.08 + random.uniform(0.01, 0.1),
                )

            await asyncio.gather(*(one(p) for p in lot_paths))

            logger.info("Collected %s parsed lot rows — building records", len(rows))

            # --- Step 3: records + price filter + export ----------------------
            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at,
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info("After min-price ($%s): %s -> %s records",
                        f"{self.min_price:,}", before, len(records))

        csv_path = self.output_dir / "gooding_auction_results.csv"
        xlsx_path = self.output_dir / "gooding_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Gooding & Company - Complete: %s records", len(records))
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

    # --- Upcoming mode -----------------------------------------------------

    async def _run_upcoming(self) -> list[dict]:
        """Scrape preview catalogues for upcoming Gooding auctions.

        Flow:
          1. Parse homepage navbar for ``/auction/<slug>`` paths.
          2. For each, fetch the auction HTML and parse the
             'View Early Consignments' button's href to extract its
             ``filtersInput`` query (``liveAuctionVenue`` + ``auctionYear``
             + ``auctionType``). Auctions without that button are skipped.
          3. Replay the site's ``GetVehiclesAndFilters`` GraphQL persisted
             query with those filters — returns vehicles with their static
             slugs. (Early-consignment lots are NOT in the app.js bundle
             yet, which is why we can't use bundle discovery here.)
          4. For each ``/lot/<slug>``, fetch the per-lot page-data and parse
             the same way past mode does.
          5. Build records with ``Auction_status='Upcoming'``. Min-price
             gate is SKIPPED — upcoming lots have no hammer price.
        """
        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            # 1) navbar → /auction paths
            home_html = await fetch_home_html(client)
            auction_paths = parse_navbar_auction_paths(home_html)
            if not auction_paths:
                logger.warning("Gooding upcoming: no /auction/* paths in navbar")
                return []
            logger.info("Navbar auctions: %s", auction_paths)

            # 2 + 3) per-auction: parse EC filters, call GraphQL
            seen_slugs: set[str] = set()
            lot_paths: list[str] = []
            for path in auction_paths:
                try:
                    ah = await fetch_auction_html(client, path=path)
                except Exception as e:
                    logger.warning("  %s HTML fetch failed: %s — skipping", path, e)
                    continue
                ec = parse_early_consignments_filters(ah)
                if not ec:
                    logger.info(
                        "  %s: no Early Consignments button — past/far-future, skipping",
                        path,
                    )
                    continue
                logger.info("  %s: EC filters=%s sort=%s", path, ec["filters"], ec["sort_by"])
                try:
                    vehicles = await iter_upcoming_vehicles_graphql(
                        client, filters=ec["filters"], sort_by=ec["sort_by"],
                    )
                except Exception as e:
                    logger.warning("  %s GraphQL failed: %s — skipping", path, e)
                    continue
                added = 0
                for v in vehicles:
                    slug = (v.get("slug") or "").strip()
                    if not slug or slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                    lot_paths.append(f"/lot/{slug}")
                    added += 1
                logger.info("  %s: %s vehicles from GraphQL (%s new)", path, len(vehicles), added)
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.2 + random.uniform(0.05, 0.2))

            if not lot_paths:
                logger.warning("Gooding upcoming: no early-consignment lots discovered")
                return []
            logger.info("Total upcoming lot paths: %s", len(lot_paths))

            if self.max_pages > 0:
                lot_paths = lot_paths[: self.max_pages]
                logger.info("Test mode: capping to %s lot(s)", len(lot_paths))

            # 4) concurrent per-lot page-data fetches (same as past mode)
            detail_conc = max(1, min(self.max_concurrent, 8))
            sem = asyncio.Semaphore(detail_conc)
            rows: list[dict] = []
            done = 0
            total = len(lot_paths)
            lock = asyncio.Lock()

            async def one(path: str) -> None:
                nonlocal done
                async with sem:
                    try:
                        payload = await fetch_page_data(client, path=path)
                        row = parse_lot_page_data(payload, path=path)
                    except Exception as e:
                        logger.debug("  lot %s failed: %s", path, e)
                        row = None
                    async with lock:
                        done += 1
                        if row:
                            rows.append(row)
                        if done % 50 == 0 or done == total:
                            logger.info("Lot fetch %s/%s (rows=%s)", done, total, len(rows))
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.08 + random.uniform(0.01, 0.1))

            await asyncio.gather(*(one(p) for p in lot_paths))
            logger.info("Collected %s upcoming-lot rows — building records", len(rows))

            # 5) build records
            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at, mode="upcoming",
            )

        # write — upcoming has no realised price; skip min-price gate
        csv_path = self.output_dir / "gooding_upcoming.csv"
        xlsx_path = self.output_dir / "gooding_upcoming.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Gooding & Company (UPCOMING) - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
