"""RM Sotheby's — results-page / upcoming-page discovery + SearchLots API + lot HTML detail.

Discovery paths by ``mode``:

``past``     GET /results/{year}                 year loop 2026 → 2002
                → auction cards with slug (e.g. ``ch25``), name, date

``upcoming`` GET /upcoming/                      → ``/auctions/{slug}/lots/`` button hrefs
             GET /auctions/{slug}/lots/          → page title (auction name) + date

Then, for both modes:
    POST /api/search/SearchLots?page=N&pageSize=40  with ``{"Auction": "CH25", …}``
         → paginated vehicle lots (``referenceId`` starts with ``r``)
    GET  /auctions/{slug}/lots/{ref}-{model-slug}   → lot detail (VIN/mileage/…)

``--max-pages N`` = max auctions to process (0 = all).
``mode='past'`` → sold-only records with hammer price.
``mode='upcoming'`` → all vehicle lots with ``Sold_status='Upcoming'`` + estimate range.
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
from shared.schemas import AUCTION_COLUMNS

from . import record_builder
from .api_client import (
    fetch_auction_lots_page_html,
    fetch_lot_detail_html,
    fetch_results_year_html,
    fetch_upcoming_html,
    post_search_lots,
)
from .config import DEFAULT_PAGE_SIZE, YEAR_END, YEAR_START
from .listing_parser import (
    is_vehicle_lot,
    merge_api_item_with_detail,
    parse_auction_meta_from_lots_page,
    parse_auctions_from_results_html,
    parse_lot_detail_html,
    parse_upcoming_slugs_from_html,
)

logger = logging.getLogger(__name__)


class RMSothebysScraper(BaseScraper):
    """Past results or upcoming lots from [RM Sotheby's](https://rmsothebys.com)."""

    SOURCE_NAME = "RM Sotheby's"
    SOURCE_DOMAIN = "rmsothebys.com"
    DATABASE = "auctions"

    def __init__(
        self,
        min_price: int = 0,
        output_dir="output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter=None,
        mode: str = "past",
    ):
        super().__init__(
            min_price=min_price,
            output_dir=output_dir,
            max_concurrent=max_concurrent,
            max_pages=max_pages,
            vehicle_filter=vehicle_filter,
        )
        if mode not in {"past", "upcoming"}:
            raise ValueError(f"mode must be 'past' or 'upcoming', got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("RM Sotheby's - Starting (mode=%s)", self.mode)
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_auctions = self.max_pages  # 0 = unlimited
        if max_auctions > 0:
            logger.info("Max auctions (test mode): %s", max_auctions)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # FX converter is shared between the early-stop loop (below) and the
            # final record builder. Created up-front so the per-page early-stop
            # check can convert local-currency lot prices to USD inline before
            # comparing against ``self.min_price``. Caches rates per (date,
            # currency) so a 40-row page typically warms one rate then reuses it.
            fx = FXConverter(client)

            # --- Step 1: discover auctions ------------------------------------
            auctions: list[dict] = []
            if self.mode == "upcoming":
                auctions = await self._discover_upcoming(client, max_auctions)
            else:
                auctions = await self._discover_past(client, max_auctions)

            logger.info("Total auctions to scrape: %s", len(auctions))

            # --- Step 2: paginate SearchLots per auction, collect vehicle rows -
            merged_rows: list[dict] = []
            for au_idx, au in enumerate(auctions, start=1):
                slug = au["slug"]
                code = slug.upper()
                name = au.get("name")
                date = au.get("date")
                logger.info("[%s/%s] Auction %s (%s) — %s",
                            au_idx, len(auctions), code, name or "?", date or "?")

                page = 1
                per_auction_items: list[dict] = []
                while True:
                    try:
                        payload = await post_search_lots(
                            client,
                            auction_code_upper=code,
                            auction_slug_lower=slug,
                            page=page,
                            page_size=DEFAULT_PAGE_SIZE,
                        )
                    except Exception as e:
                        logger.warning("  SearchLots page %s for %s failed: %s", page, code, e)
                        break

                    items = _extract_items(payload)
                    if not items:
                        break

                    kept = [it for it in items if is_vehicle_lot(it)]
                    per_auction_items.extend(kept)
                    logger.info("  Page %s: %s items (%s vehicles)", page, len(items), len(kept))

                    # Early-stop: SortBy=Value is descending, so once the
                    # highest USD-equivalent price on a full page falls below
                    # ``min_price`` every later page in this auction is too.
                    # Local prices are FX-converted inline (USD lots are a
                    # no-op; EUR/GBP/CHF are looked up against today's rate
                    # via FXConverter's cache).
                    if self.min_price > 0 and kept and len(items) >= DEFAULT_PAGE_SIZE:
                        page_max_usd = await self._page_max_usd(fx, kept, date)
                        if page_max_usd is not None and page_max_usd < self.min_price:
                            logger.info(
                                "  Page %s top USD price ($%s) < floor ($%s) - "
                                "early stop for auction %s",
                                page, f"{page_max_usd:,}",
                                f"{self.min_price:,}", code,
                            )
                            break

                    if len(items) < DEFAULT_PAGE_SIZE:
                        break
                    page += 1
                    await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.4 + random.uniform(0.05, 0.3))

                logger.info("  Auction %s: %s vehicle lots", code, len(per_auction_items))

                # --- Step 3: concurrent lot detail fetches ---------------------
                detail_conc = max(1, min(self.max_concurrent, 5))
                sem = asyncio.Semaphore(detail_conc)
                details: dict[str, dict | None] = {}
                done = 0
                total = len(per_auction_items)

                async def one(it: dict) -> None:
                    nonlocal done
                    link = str(it.get("link") or "").strip()
                    if not link:
                        return
                    async with sem:
                        try:
                            html_txt = await fetch_lot_detail_html(client, link)
                            parsed = parse_lot_detail_html(html_txt)
                        except Exception as e:
                            logger.debug("    Detail %s failed: %s", link, e)
                            parsed = None
                        details[link] = parsed
                        done += 1
                        if done % 25 == 0 or done == total:
                            logger.info("  Detail fetch %s/%s", done, total)
                    await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.25 + random.uniform(0.03, 0.2))

                await asyncio.gather(*(one(it) for it in per_auction_items))

                for it in per_auction_items:
                    link = str(it.get("link") or "").strip()
                    merged = merge_api_item_with_detail(
                        auction_slug=slug,
                        auction_code_upper=code,
                        item=it,
                        detail=details.get(link),
                        auction_name=name,
                        auction_date=date,
                    )
                    merged_rows.append(merged)

            logger.info("Collected %s merged vehicle rows — building records", len(merged_rows))

            # --- Step 4: records + price filter + export -----------------------
            # ``fx`` already created at the top of run() for the early-stop loop.
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                merged_rows, fx, self.vehicle_filter, scraped_at, mode=self.mode,
            )

            before = len(records)
            if self.mode == "past":
                records = [r for r in records if self._row_meets_min_price(r)]
                logger.info("After min-price ($%s): %s -> %s records",
                            f"{self.min_price:,}", before, len(records))
            else:
                logger.info("Upcoming mode — %s records (min-price filter skipped)", before)

        suffix = "upcoming" if self.mode == "upcoming" else "auction_results"
        csv_path = self.output_dir / f"rm_sothebys_{suffix}.csv"
        xlsx_path = self.output_dir / f"rm_sothebys_{suffix}.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("RM Sotheby's - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    async def _discover_past(self, client, max_auctions: int) -> list[dict]:
        auctions: list[dict] = []
        seen_slugs: set[str] = set()
        for year in range(YEAR_END, YEAR_START - 1, -1):
            try:
                html = await fetch_results_year_html(client, year)
            except Exception as e:
                logger.warning("Failed /results/%s: %s — skipping year", year, e)
                continue
            year_auctions = parse_auctions_from_results_html(html)
            new = 0
            for a in year_auctions:
                slug = (a.get("slug") or "").lower()
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                auctions.append(a)
                new += 1
            logger.info("Year %s: %s auctions (%s new, %s total)",
                        year, len(year_auctions), new, len(auctions))
            if max_auctions > 0 and len(auctions) >= max_auctions:
                auctions = auctions[:max_auctions]
                logger.info("Limited to %s auction(s) for test run", max_auctions)
                break
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.1, 0.4))
        return auctions

    async def _discover_upcoming(self, client, max_auctions: int) -> list[dict]:
        """``/upcoming/`` → slugs; each ``/auctions/{slug}/lots/`` → title + date."""
        try:
            upcoming_html = await fetch_upcoming_html(client)
        except Exception as e:
            logger.error("Failed /upcoming/: %s", e)
            return []
        slugs = parse_upcoming_slugs_from_html(upcoming_html)
        logger.info("/upcoming/: %s auction slugs discovered", len(slugs))
        if max_auctions > 0:
            slugs = slugs[:max_auctions]
            logger.info("Limited to %s upcoming auction(s)", max_auctions)

        auctions: list[dict] = []
        for slug in slugs:
            try:
                page_html = await fetch_auction_lots_page_html(client, slug)
                meta = parse_auction_meta_from_lots_page(page_html)
            except Exception as e:
                logger.warning("  /auctions/%s/lots/ meta fetch failed: %s", slug, e)
                meta = {"name": None, "date": None}
            auctions.append({"slug": slug, "name": meta.get("name"), "date": meta.get("date")})
            logger.info("  %s — %s (%s)", slug, meta.get("name") or "?", meta.get("date") or "?")
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.25 + random.uniform(0.05, 0.2))
        return auctions

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

    async def _page_max_usd(
        self, fx: FXConverter, items: list[dict], auction_date: str | None,
    ) -> int | None:
        """Highest USD-equivalent price across one page of SearchLots items.

        Used by the per-auction early-stop check. Walks each item, parses its
        local-currency value via ``parse_value_fields``, FX-converts to USD,
        and returns the maximum. Returns ``None`` if no item on the page has
        a parseable price (e.g. an auction whose lots are all "Reserve Met"
        with no public hammer) — caller treats that as "don't early-stop".
        """
        from .listing_parser import parse_value_fields
        date_str = auction_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        best: int | None = None
        for it in items:
            vf = parse_value_fields(
                str(it.get("value") or ""),
                str(it.get("valueType") or ""),
                it.get("sold"),
                str(it.get("preSaleEstimate") or ""),
            )
            # Prefer the realised hammer; fall back to the high estimate so
            # we don't break out of an auction just because nothing has sold
            # yet (also covers ``StillForSaleOnly`` runs).
            local = vf.get("selling_price") or vf.get("estimate_high") or vf.get("estimate_low")
            if local is None:
                continue
            currency = (vf.get("currency") or "USD").upper()
            try:
                usd, _rate = await fx.convert_to_usd(int(local), currency, date_str)
            except Exception:
                continue
            if usd is None:
                continue
            if best is None or int(usd) > best:
                best = int(usd)
        return best


def _extract_items(payload: dict) -> list[dict]:
    """SearchLots returns items under ``items`` / ``Items`` / ``results`` / ``data``."""
    if not isinstance(payload, dict):
        return []
    for key in ("items", "Items", "results", "Results", "data", "Data"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    return []
