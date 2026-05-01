"""AutoScout24 — EU marketplace classifieds (Next.js ``_next/data`` JSON).

Flow:
    GET ``/lst?...`` HTML → extract ``buildId``
    GET ``/_next/data/{buildId}/lst.json?...&page=N`` → ``listings`` (sort=price desc)
    GET ``/_next/data/{buildId}/details/{slug}.json`` per offer → VIN, description, specs

``--mode used|new|all`` maps to ``ustate=`` (U / N / N%2CU).
``--max-pages`` caps list pages (site often caps at ~200).
``--min-price`` is in USD (converted from EUR/GBP/… via Frankfurter).

Source column: ``autoscout24.com``.  ``listing_segment`` stores ``used`` / ``new``
when mode is narrowed; omitted when ``all``.
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

from . import record_builder
from .api_client import fetch_build_id, fetch_detail_json, fetch_list_json
from .config import HARD_PAGE_CAP, LIST_QUERY_BASE, MODE_ALL, MODE_NEW, MODE_USED, SITE_ORIGIN
from .listing_parser import parse_detail, parse_list_item

logger = logging.getLogger(__name__)

_MODE_TO_USTATE = {
    MODE_USED: "U",
    MODE_NEW: "N",
    MODE_ALL: "N%2CU",
}


class AutoScout24Scraper(BaseScraper):
    """Classifieds from [AutoScout24](https://www.autoscout24.com)."""

    SOURCE_NAME = "AutoScout24"
    SOURCE_DOMAIN = "autoscout24.com"
    DATABASE = "classifieds"

    def __init__(
        self,
        min_price: int = 0,
        output_dir="output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter=None,
        mode: str = MODE_USED,
    ):
        super().__init__(
            min_price=min_price,
            output_dir=output_dir,
            max_concurrent=max_concurrent,
            max_pages=max_pages,
            vehicle_filter=vehicle_filter,
        )
        if mode not in (MODE_USED, MODE_NEW, MODE_ALL):
            raise ValueError(f"mode must be {MODE_USED!r}, {MODE_NEW!r}, or {MODE_ALL!r}, got {mode!r}")
        self.mode = mode

    async def run(self) -> list[dict]:
        ustate = _MODE_TO_USTATE[self.mode]
        list_path = f"/lst?{LIST_QUERY_BASE}&ustate={ustate}&page=1"
        seg = None if self.mode == MODE_ALL else self.mode

        logger.info("=" * 60)
        logger.info("AutoScout24 (%s) — starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited; cap=%s)", self.max_pages, HARD_PAGE_CAP)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        today_iso = datetime.now(tz=timezone.utc).date().isoformat()
        scraped_at = get_scraped_at()

        async with httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
            try:
                build_id = await fetch_build_id(client, list_path=list_path)
            except Exception as e:
                logger.error("buildId discovery failed: %s", e)
                return []

            logger.info("Next.js buildId: %s", build_id)
            referer = f"{SITE_ORIGIN}{list_path}"

            flats: list[dict] = []
            search_id: str | None = None
            user_cap = self.max_pages if self.max_pages > 0 else HARD_PAGE_CAP
            user_cap = min(user_cap, HARD_PAGE_CAP)
            pages_to_scan = user_cap

            for page in range(1, user_cap + 1):
                if page > pages_to_scan:
                    break
                try:
                    payload = await fetch_list_json(
                        client,
                        build_id=build_id,
                        page=page,
                        ustate=ustate,
                        search_id=search_id,
                        referer=referer,
                    )
                except Exception as e:
                    logger.error("List page %s failed: %s", page, e)
                    break

                pp = payload.get("pageProps") or {}
                if page == 1:
                    search_id = pp.get("trackingId") or search_id
                    nres = pp.get("numberOfResults")
                    np = pp.get("numberOfPages")
                    logger.info(
                        "Search trackingId=%s results=%s pages=%s",
                        search_id, nres, np,
                    )
                    if isinstance(np, int) and np > 0:
                        pages_to_scan = min(user_cap, np)

                items = pp.get("listings") or []
                if not items:
                    logger.info("Empty listings on page %s — stop", page)
                    break

                kept = 0
                for item in items:
                    row = parse_list_item(item)
                    if not row:
                        continue
                    title = row.get("title") or ""
                    if self.vehicle_filter and title:
                        if not self.vehicle_filter.match(title, ""):
                            continue
                    flats.append(row)
                    kept += 1

                logger.info("Page %s: %s listings, %s kept (total %s)", page, len(items), kept, len(flats))
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.35 + random.uniform(0.05, 0.25))

            logger.info("Collected %s cards — fetching details", len(flats))

            detail_conc = max(1, min(self.max_concurrent, 5))
            sem = asyncio.Semaphore(detail_conc)
            details: dict[str, dict] = {}
            done = 0
            total = len(flats)

            async def one(card: dict) -> None:
                nonlocal done
                key = card["record_id"]
                async with sem:
                    raw = await fetch_detail_json(
                        client, build_id=build_id, offer_path=card["offer_path"],
                    )
                    details[key] = parse_detail(raw)
                    done += 1
                    if done % 15 == 0 or done == total:
                        logger.info("Detail fetch %s/%s", done, total)
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.25 + random.uniform(0.03, 0.15))

            await asyncio.gather(*(one(c) for c in flats))

            fx = FXConverter(client)
            records: list[dict] = []
            for card in flats:
                d = details.get(card["record_id"]) or {}
                rec = await record_builder.build_record(
                    flat=card,
                    detail=d,
                    fx=fx,
                    scraped_at=scraped_at,
                    today_iso=today_iso,
                    listing_segment=seg,
                )
                usd = rec.get("Asking_price_USD")
                if self.min_price > 0:
                    if usd is None or usd < self.min_price:
                        continue
                records.append(rec)

            logger.info("After min-price ($%s): %s records", f"{self.min_price:,}", len(records))

        csv_path = self.output_dir / f"autoscout24_{self.mode}_classifieds.csv"
        xlsx_path = self.output_dir / f"autoscout24_{self.mode}_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("AutoScout24 — complete: %s records", len(records))
        return records
