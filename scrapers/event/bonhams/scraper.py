"""Bonhams Cars — Typesense auction discovery + Next.js lot pages + lot detail HTML.

Discovery path:
    POST /search-proxy/multi_search (collection ``auctions-search``)
         filter ``biddingStatus:=EN && brand:=bonhams-cars && auctionType:=[ONLINE,PUBLIC]``
         → paginated 24 ended auctions per page, sorted newest first
    GET  /auction/{id}/{slug}/?page=N
         → ``__NEXT_DATA__.props.pageProps.lotData.auctionLots`` (48 per page)
    GET  /auction/{id}/lot/{lotNo}/{slug}/
         → ``__NEXT_DATA__.props.pageProps.lot`` (full description + price fields)

--max-pages N = max auctions to process (0 = all ended Bonhams Cars auctions).
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
    fetch_auction_lots_page_html,
    fetch_lot_detail_html,
    post_auctions_search,
)
from .config import AUCTION_LOTS_PER_PAGE, SITE_ORIGIN
from .listing_parser import (
    build_lot_detail_url,
    is_car_lot,
    parse_auction_hits,
    parse_lot_detail_next_data,
    parse_lots_from_auction_page,
)

logger = logging.getLogger(__name__)


class BonhamsScraper(BaseScraper):
    """Past results or upcoming lots from [Bonhams Cars](https://cars.bonhams.com)."""

    SOURCE_NAME = "Bonhams"
    SOURCE_DOMAIN = "cars.bonhams.com"
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
        logger.info("Bonhams - Starting (mode=%s)", self.mode)
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        max_auctions = self.max_pages  # 0 = unlimited
        if max_auctions > 0:
            logger.info("Max auctions (test mode): %s", max_auctions)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:

            # --- Step 1: paginate Typesense auctions-search -------------------
            auctions: list[dict] = []
            seen_ids: set[str] = set()
            page = 0  # Typesense uses 1-indexed pages; start at 1.
            total_found: int | None = None

            while True:
                page += 1
                try:
                    payload = await post_auctions_search(client, page=page, mode=self.mode)
                except Exception as e:
                    logger.warning("auctions-search page %s failed: %s", page, e)
                    break

                hits, found = parse_auction_hits(payload)
                if total_found is None:
                    total_found = found
                    label = "upcoming" if self.mode == "upcoming" else "ended"
                    logger.info("Typesense reports %s %s auctions total", total_found, label)
                if not hits:
                    break

                new = 0
                for au in hits:
                    if au["id"] in seen_ids:
                        continue
                    seen_ids.add(au["id"])
                    auctions.append(au)
                    new += 1
                logger.info("Auctions page %s: %s hits (%s new, %s total)",
                            page, len(hits), new, len(auctions))

                if max_auctions > 0 and len(auctions) >= max_auctions:
                    auctions = auctions[:max_auctions]
                    logger.info("Limited to %s auction(s) for test run", max_auctions)
                    break
                if total_found and len(auctions) >= total_found:
                    break

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.3 + random.uniform(0.05, 0.3))

            logger.info("Total auctions to scrape: %s", len(auctions))

            # --- Step 2: paginate auction lots pages, collect car lots --------
            merged_rows: list[dict] = []
            for au_idx, au in enumerate(auctions, start=1):
                aid = au["id"]
                slug = au["slug"]
                logger.info("[%s/%s] Auction %s (%s) — %s", au_idx, len(auctions),
                            aid, slug, au.get("title"))

                lots_for_auction: list[dict] = []
                page_no = 0
                while True:
                    page_no += 1
                    try:
                        html = await fetch_auction_lots_page_html(
                            client, auction_id=aid, slug=slug, page=page_no,
                        )
                    except Exception as e:
                        logger.warning("  Lots page %s for %s failed: %s", page_no, aid, e)
                        break

                    lots, nb = parse_lots_from_auction_page(html)
                    if not lots:
                        break
                    car_lots = [lot for lot in lots if is_car_lot(lot)]
                    lots_for_auction.extend(car_lots)
                    logger.info("  Page %s: %s lots (%s cars, %s total nb)",
                                page_no, len(lots), len(car_lots), nb)

                    if len(lots) < AUCTION_LOTS_PER_PAGE:
                        break
                    await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.25 + random.uniform(0.03, 0.2))

                if not lots_for_auction:
                    logger.info("  Auction %s: 0 car lots — skipping details", aid)
                    continue

                logger.info("  Auction %s: %s car lots — fetching details",
                            aid, len(lots_for_auction))

                # --- Step 3: concurrent lot detail fetches --------------------
                detail_conc = max(1, min(self.max_concurrent, 6))
                sem = asyncio.Semaphore(detail_conc)
                details: dict[str, dict | None] = {}
                done = 0
                total = len(lots_for_auction)

                async def one(lot: dict) -> None:
                    nonlocal done
                    lot_no = str((lot.get("lotNo") or {}).get("full") or lot.get("lotId") or "")
                    lot_slug = str(lot.get("slug") or "")
                    key = f"{lot_no}/{lot_slug}"
                    if not lot_no or not lot_slug:
                        return
                    async with sem:
                        try:
                            html_txt = await fetch_lot_detail_html(
                                client, auction_id=aid, lot_no=lot_no, lot_slug=lot_slug,
                            )
                            parsed = parse_lot_detail_next_data(html_txt)
                        except Exception as e:
                            logger.debug("    detail %s failed: %s", key, e)
                            parsed = None
                        details[key] = parsed
                        done += 1
                        if done % 25 == 0 or done == total:
                            logger.info("  Detail fetch %s/%s", done, total)
                    await asyncio.sleep(
                        REQUEST_DELAY_SECONDS * 0.15 + random.uniform(0.02, 0.15),
                    )

                await asyncio.gather(*(one(lot) for lot in lots_for_auction))

                for lot in lots_for_auction:
                    lot_no = str((lot.get("lotNo") or {}).get("full") or lot.get("lotId") or "")
                    lot_slug = str(lot.get("slug") or "")
                    key = f"{lot_no}/{lot_slug}"
                    detail = details.get(key)
                    if not detail:
                        continue

                    record_id = f"bon_{aid}_{lot_no}"
                    listing_url = build_lot_detail_url(aid, lot_no, lot_slug)
                    merged_rows.append({
                        **detail,
                        "record_id": record_id,
                        "lot_no": lot_no or detail.get("lot_no"),
                        "auction_id": aid,
                        "auction_slug": slug,
                        "auction_title": au.get("title"),
                        "country": au.get("country"),
                        "listing_url": listing_url,
                    })

                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.3 + random.uniform(0.05, 0.2))

            logger.info("Collected %s merged car-lot rows — building records",
                        len(merged_rows))

            # --- Step 4: records + price filter + export ----------------------
            fx = FXConverter(client)
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
        csv_path = self.output_dir / f"bonhams_{suffix}.csv"
        xlsx_path = self.output_dir / f"bonhams_{suffix}.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, xlsx_path, AUCTION_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("Bonhams - Complete: %s records", len(records))
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
