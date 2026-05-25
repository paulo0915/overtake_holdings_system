"""Barrett-Jackson — previous docket results API (sold vehicle history)."""

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
    discover_non_included_slugs,
    discover_upcoming_event_slugs,
    fetch_previous_docket_page,
    fetch_upcoming_docket_page,
)
from .config import DEFAULT_PAGE_SIZE, DEFAULT_RESULT_TYPE, DEFAULT_SORT
from .listing_parser import parse_docket_entry

logger = logging.getLogger(__name__)


class BarrettJacksonScraper(BaseScraper):
    """Auction results from [Barrett-Jackson](https://www.barrett-jackson.com).

    ``mode='past'`` (default) — fetches sold lots via ``/api/previous-docket-results``.
    ``mode='upcoming'`` — discovers events with published preview dockets on
    ``/auctions``, then paginates ``/api/docket`` per event slug.
    """

    SOURCE_NAME = "Barrett-Jackson"
    SOURCE_DOMAIN = "barrett-jackson.com"
    DATABASE = "auctions"

    def __init__(self, *args, mode: str = "past", **kwargs):
        super().__init__(*args, **kwargs)
        if mode not in ("past", "upcoming"):
            raise ValueError(f"Barrett-Jackson mode must be 'past' or 'upcoming', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Barrett-Jackson (%s) - Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max API pages (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        if self.mode == "upcoming":
            return await self._run_upcoming()

        rows: list[dict] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            non_included_slugs = await discover_non_included_slugs(client)

            try:
                first = await fetch_previous_docket_page(
                    client,
                    page=1,
                    page_size=DEFAULT_PAGE_SIZE,
                    sort=DEFAULT_SORT,
                    result_type=DEFAULT_RESULT_TYPE,
                    non_included_slugs=non_included_slugs,
                )
            except Exception as e:
                logger.error("API page 1 error: %s", e)
                return []

            meta = first.get("meta") or {}
            pag = meta.get("pagination") or {}
            try:
                page_count = int(pag.get("pageCount") or 1)
            except (TypeError, ValueError):
                page_count = 1
            total_hits = pag.get("total")

            max_page = page_count
            if self.max_pages > 0:
                max_page = min(page_count, self.max_pages)

            logger.info(
                "Docket API: total=%s, page_count=%s, fetching %s page(s), size=%s",
                total_hits,
                page_count,
                max_page,
                DEFAULT_PAGE_SIZE,
            )

            def _append_page(data: dict) -> None:
                for item in data.get("data") or []:
                    row = parse_docket_entry(item)
                    if row:
                        rows.append(row)

            def _page_all_below_floor(data: dict) -> bool:
                """Sort is ``price_desc`` and prices are USD on this source,
                so once a page's highest hammer is below ``min_price``, every
                later page is too. Returns True only when min_price > 0 and
                no item on the page reaches the floor (skips items with no
                price_decimal — those are TBD/no-sale lots that get sorted
                to the tail by the API).
                """
                if self.min_price <= 0:
                    return False
                items = data.get("data") or []
                for item in items:
                    p = ((item.get("attributes") or {}).get("price_decimal"))
                    if isinstance(p, (int, float)) and p >= self.min_price:
                        return False
                return bool(items)  # don't claim "below floor" on empty pages

            _append_page(first)
            if _page_all_below_floor(first):
                logger.info("Page 1 already below min-price floor - stopping")
                max_page = 1

            for page in range(2, max_page + 1):
                await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))
                try:
                    data = await fetch_previous_docket_page(
                        client,
                        page=page,
                        page_size=DEFAULT_PAGE_SIZE,
                        sort=DEFAULT_SORT,
                        result_type=DEFAULT_RESULT_TYPE,
                        non_included_slugs=non_included_slugs,
                    )
                except Exception as e:
                    logger.warning("API page %s: %s - stopping pagination", page, e)
                    break
                _append_page(data)
                if _page_all_below_floor(data):
                    logger.info(
                        "Page %s all below $%s floor - early stop (saved %s pages)",
                        page, f"{self.min_price:,}", max_page - page,
                    )
                    break
                if page % 10 == 0 or page == max_page:
                    logger.info("Fetched page %s/%s - %s lots", page, max_page, len(rows))

            logger.info("Collected %s lots; building records", len(rows))

            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at,
            )

            before = len(records)
            records = [r for r in records if self._row_meets_min_price(r)]
            logger.info(
                "After min-price ($%s): %s -> %s records",
                f"{self.min_price:,}",
                before,
                len(records),
            )

        csv_path = self.output_dir / "barrett_jackson_auction_results.csv"
        xlsx_path = self.output_dir / "barrett_jackson_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Barrett-Jackson - Complete: %s records", len(records))
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

    # --- Upcoming mode -------------------------------------------------

    async def _run_upcoming(self) -> list[dict]:
        """Scrape upcoming dockets from ``/api/docket``.

        Flow: discover event slugs on ``/auctions`` (only events whose
        preview catalog is published render a ``/docket`` href), then
        paginate per-event via ``?slug={slug}&page={N}`` until empty.

        Min-price filter is best-effort only — upcoming lots have no
        hammer or estimate from this source, so we keep all rows unless
        the caller explicitly sets ``min_price > 0``, in which case we
        skip the floor check (there's nothing to compare against).
        """
        rows: list[dict] = []

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            slugs = await discover_upcoming_event_slugs(client)
            if not slugs:
                logger.warning("BJ upcoming: no events with published dockets found")
                return []
            if self.max_pages > 0 and len(slugs) > self.max_pages:
                # Reuse max_pages as an event cap in test mode (matches the
                # convention used by other event-auction scrapers).
                logger.info("BJ upcoming: capping at %s events (test mode)", self.max_pages)
                slugs = slugs[: self.max_pages]

            for idx, slug in enumerate(slugs, start=1):
                logger.info("[%s/%s] Fetching upcoming docket: %s", idx, len(slugs), slug)
                page = 1
                while True:
                    try:
                        data = await fetch_upcoming_docket_page(
                            client, slug=slug, page=page, page_size=DEFAULT_PAGE_SIZE,
                        )
                    except Exception as e:
                        logger.warning("BJ upcoming %s page %s failed: %s", slug, page, e)
                        break

                    items = data.get("data") or []
                    if not items:
                        break
                    page_added = 0
                    for item in items:
                        row = parse_docket_entry(item)
                        if row:
                            rows.append(row)
                            page_added += 1
                    logger.info(
                        "  %s page %s: %s items, total rows %s",
                        slug, page, page_added, len(rows),
                    )
                    # Pagination heuristic: meta.pagination may be present;
                    # if not, stop when a short page comes back.
                    meta = data.get("meta") or {}
                    pag = meta.get("pagination") or {}
                    page_count = pag.get("pageCount")
                    if isinstance(page_count, int) and page >= page_count:
                        break
                    if len(items) < DEFAULT_PAGE_SIZE:
                        break
                    page += 1
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.1, 0.45))

            logger.info("Collected %s upcoming lots; building records", len(rows))
            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                rows, fx, self.vehicle_filter, scraped_at, mode="upcoming",
            )

        # Upcoming has no realised price to filter on; skip min-price gate.
        csv_path = self.output_dir / "barrett_jackson_upcoming.csv"
        xlsx_path = self.output_dir / "barrett_jackson_upcoming.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Barrett-Jackson (UPCOMING) - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
