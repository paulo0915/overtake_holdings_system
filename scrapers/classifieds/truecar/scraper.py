"""TrueCar — classifieds scraper orchestrator (used + new modes).

Discovery flow:
    POST graph.truecar.com/graphql (``getMarketplaceSearch``)
        → paginate 30 listings per call, sorted ``PRICE_DESC``.
    GET  /used-cars-for-sale/listing/<vin>/
        → HTML VDP. Parse ``__NEXT_DATA__`` → ``__APOLLO_STATE__`` →
          ``Listing:<id>.vehicle.sellerNotes`` for dealer prose.

``--mode used|new`` picks the ``filters.condition`` enum.
``--max-pages N`` = max GraphQL search pages (30 listings each).
``--min-price USD`` = stops once the DESC-sorted price drops below.

Source tag is scoped per mode (``truecar.com/used`` vs ``truecar.com/new``)
so ``mark_removed_classifieds`` only flips rows for the exact mode we just
scraped — used + new are disjoint inventories.

Bot-protection notes:
    TrueCar sits behind PerimeterX. Datacenter IPs get blocked at the
    homepage level (403 before any GraphQL). When that happens the scraper
    automatically falls back to a residential proxy from the Supabase
    ``proxies`` table. Add proxies there if TrueCar returns 0 records.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS
from shared.export import export_to_csv, export_to_excel
from shared.normalizer import get_scraped_at
from shared.proxies import get_random_proxy, mark_bad
from shared.schemas import CLASSIFIEDS_COLUMNS
from shared.vehicle_filter import VehicleFilter

from . import record_builder
from .api_client import fetch_detail_html, new_session, post_search_page, warmup
from .config import DEFAULT_PAGE_SIZE, HARD_RESULT_CAP, MODE_NEW, MODE_USED
from .listing_parser import parse_detail_payload, parse_list_edge

logger = logging.getLogger(__name__)


class TrueCarScraper(BaseScraper):
    """Classifieds from [TrueCar](https://www.truecar.com)."""

    SOURCE_NAME = "TrueCar"
    SOURCE_DOMAIN = "truecar.com"
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
            raise ValueError(f"TrueCar mode must be 'used' or 'new', got {mode!r}")
        self.mode = mode

    async def _make_working_session(self):
        """Return an AsyncSession that has passed PerimeterX warmup.

        Tries a direct connection first. If the homepage 403s (datacenter IP
        blocked), fetches a residential proxy from Supabase and retries once.
        Returns ``(session, proxy_used_or_None)`` with warmup already done,
        or ``(session, None)`` where the session has no working warmup when
        both attempts fail (caller should abort gracefully).
        """
        # --- Direct attempt ---
        direct = new_session()
        await direct.__aenter__()
        try:
            await warmup(direct, mode=self.mode)
            return direct, None  # success — caller owns the context manager
        except RuntimeError as e:
            if "datacenter IP blocked" not in str(e) and "403" not in str(e):
                logger.error("TrueCar warmup failed: %s", e)
                return direct, None  # non-PX failure; return open session
            logger.warning("Direct IP blocked by PerimeterX — trying residential proxy")
        await direct.__aexit__(None, None, None)

        # --- Proxy attempt ---
        proxy = get_random_proxy()
        if not proxy:
            logger.error(
                "No proxies in Supabase 'proxies' table — "
                "add residential proxies to unblock TrueCar"
            )
            fallback = new_session()
            await fallback.__aenter__()
            return fallback, None

        logger.info("Using proxy: %s", proxy.server)
        proxied = new_session(proxy_url=proxy.url)
        await proxied.__aenter__()
        try:
            await warmup(proxied, mode=self.mode)
            return proxied, proxy
        except Exception as e:
            logger.error("TrueCar warmup via proxy failed: %s — marking bad", e)
            mark_bad(proxy.url)
            await proxied.__aexit__(None, None, None)
            fallback = new_session()
            await fallback.__aenter__()
            return fallback, None

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("TrueCar (%s) — Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited)", self.max_pages)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        client, proxy = await self._make_working_session()
        try:
            flat_rows, records = await self._scrape(client)
        finally:
            await client.__aexit__(None, None, None)

        # CSV / XLSX for spot-checking. Mode in filename so used + new don't
        # overwrite each other.
        csv_path = self.output_dir / f"truecar_{self.mode}_classifieds.csv"
        xlsx_path = self.output_dir / f"truecar_{self.mode}_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("TrueCar (%s) - Complete: %s records", self.mode.upper(), len(records))
        logger.info("=" * 60)
        return records

    async def _scrape(self, client) -> tuple[list[dict], list[dict]]:
        # --- Step 1: paginate GraphQL search ----------------------------------
        flat_rows: list[dict] = []
        offset = 0
        page = 0
        total_reported: int | None = None
        while True:
            page += 1
            try:
                payload = await post_search_page(
                    client, mode=self.mode, first=DEFAULT_PAGE_SIZE, offset=offset,
                )
            except Exception as e:
                logger.error("Search page %s failed: %s", page, e)
                break

            edges = payload.get("edges") or []
            if total_reported is None:
                total_reported = payload.get("totalCount")
                logger.info(
                    "GraphQL reports %s total listings (capped at %s)",
                    total_reported, HARD_RESULT_CAP,
                )

            if not edges:
                break

            page_added = 0
            stop_low_price = False
            for edge in edges:
                flat = parse_list_edge(edge)
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
                "Page %s (offset %s): %s edges -> %s kept (running total %s)",
                page, offset, len(edges), page_added, len(flat_rows),
            )

            if stop_low_price:
                logger.info(
                    "Hit min-price floor ($%s) - stopping pagination", f"{self.min_price:,}",
                )
                break
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            if self.max_pages > 0 and page >= self.max_pages:
                logger.info("Max pages (%s) reached - stopping", self.max_pages)
                break
            if offset + DEFAULT_PAGE_SIZE >= HARD_RESULT_CAP:
                logger.info("Reached TrueCar 10k result cap - stopping")
                break

            offset += DEFAULT_PAGE_SIZE
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.25))

        logger.info("Collected %s listings - fetching details", len(flat_rows))

        # --- Step 2: concurrent detail fetches --------------------------------
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
                    next_data, html = await fetch_detail_html(client, vin=flat["vin"])
                    parsed = parse_detail_payload(next_data, html=html)
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

        # --- Step 3: build records --------------------------------------------
        scraped_at = get_scraped_at()
        today_iso = datetime.now(tz=timezone.utc).date().isoformat()
        records: list[dict] = []
        for flat in flat_rows:
            rec = record_builder.build_record(
                flat=flat,
                detail=details.get(flat["record_id"]),
                scraped_at=scraped_at,
                today_iso=today_iso,
                source="truecar.com",
                listing_segment=self.mode,
            )
            records.append(rec)

        logger.info("Built %s classifieds records", len(records))
        return flat_rows, records
