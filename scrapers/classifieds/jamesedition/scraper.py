"""JamesEdition — classifieds scraper orchestrator.

Discovery flow:
    GET /cars/all?order=price_desc&page=N
        → HTML SRP with 30 ``ListingCard`` divs each carrying rich
          ``data-*`` attributes (id, brand, model, source price/currency,
          USD-converted price, country, city).
    GET /cars/<brand>/<model>/<slug>-for-sale-<id>
        → HTML detail page with VIN, colours, engine, gearbox, mileage,
          dealer name (from JSON-LD Product), and the "About This Car"
          dealer description.

Cloudflare rate-limits aggressive scrapers, so concurrency is capped low
and there's a throttle between requests.

``--max-pages N`` = max SRP pages (30 listings each).
``--min-price USD`` = stop once JE's USD-converted price drops below.
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
from shared.schemas import CLASSIFIEDS_COLUMNS

from . import record_builder
from .api_client import JESession
from .config import DEFAULT_PAGE_SIZE, HARD_PAGE_CAP
from .listing_parser import iter_listings, parse_detail_html, parse_list_tile

logger = logging.getLogger(__name__)


class JamesEditionScraper(BaseScraper):
    """Classifieds from [JamesEdition](https://www.jamesedition.com)."""

    SOURCE_NAME = "JamesEdition"
    SOURCE_DOMAIN = "jamesedition.com"
    DATABASE = "classifieds"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("JamesEdition - Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Max pages: %s (0=unlimited; site cap=%s)", self.max_pages, HARD_PAGE_CAP)
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with JESession() as session:
            # --- Step 1: paginate SRP ---------------------------------------
            flat_rows: list[dict] = []
            page = 0
            while True:
                page += 1
                # Retry SRP pages with a fresh proxy on CF challenge — the
                # proxy pool may have a throttled IP from a prior run.
                html = None
                for srp_attempt in range(6):
                    try:
                        html = await session.fetch_list_page(page=page)
                        break
                    except RuntimeError as e:
                        if "CF challenge" in str(e) and srp_attempt < 5:
                            logger.warning(
                                "CF challenge on SRP page %s (attempt %d) — rotating proxy",
                                page, srp_attempt + 1,
                            )
                            await session.rotate_proxy()
                        else:
                            logger.error("SRP page %s failed: %s", page, e)
                            break
                    except Exception as e:
                        err_lower = str(e).lower()
                        retryable = any(
                            kw in err_lower for kw in ("timed out", "timeout", "connection", "refused")
                        )
                        if retryable and srp_attempt < 5:
                            logger.warning(
                                "SRP page %s network error (attempt %d) — rotating proxy: %s",
                                page, srp_attempt + 1, e,
                            )
                            await session.rotate_proxy()
                        else:
                            logger.error("SRP page %s failed: %s", page, e)
                            break
                if html is None:
                    break

                tiles = list(iter_listings(html))
                if not tiles:
                    break

                page_added = 0
                stop_low_price = False
                for tile in tiles:
                    flat = parse_list_tile(tile)
                    if not flat:
                        continue
                    price_usd = flat.get("asking_price_usd_from_list")
                    # Skip rows with no price (JE dealers often omit it —
                    # those can't be min-price filtered). Keep them though,
                    # since the title is still useful.
                    if price_usd is not None and price_usd < self.min_price:
                        stop_low_price = True
                        break
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    flat_rows.append(flat)
                    page_added += 1

                logger.info(
                    "Page %s: %s tiles -> %s kept (running total %s)",
                    page, len(tiles), page_added, len(flat_rows),
                )

                if stop_low_price:
                    logger.info(
                        "Hit min-price floor ($%s) - stopping pagination", f"{self.min_price:,}",
                    )
                    break
                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info("Max pages (%s) reached - stopping", self.max_pages)
                    break
                if page >= HARD_PAGE_CAP:
                    logger.info("Reached site pagination cap - stopping")
                    break
                # Throttle between SRP pages — CF flags rapid scrape.
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.7 + random.uniform(0.2, 0.6))

            logger.info("Collected %s listings - fetching details", len(flat_rows))

            # --- Step 2: detail fetches -------------------------------------
            # Fetch details sequentially. Rotate the proxy every 200 fetches
            # to avoid JamesEdition/CF per-IP rate limiting (the proxy IP gets
            # throttled after ~200-260 requests, causing 45s timeouts).
            PROXY_ROTATE_EVERY = 200
            details: dict[str, dict] = {}
            total = len(flat_rows)
            for idx, flat in enumerate(flat_rows, 1):
                if idx > 1 and (idx - 1) % PROXY_ROTATE_EVERY == 0:
                    await session.rotate_proxy()
                key = flat["record_id"]
                try:
                    raw = await session.fetch_detail_html(url=flat["listing_url"])
                    parsed = parse_detail_html(raw) if raw else {}
                except Exception as e:
                    logger.debug("detail %s failed: %s", key, e)
                    parsed = {}
                details[key] = parsed
                if idx % 20 == 0 or idx == total:
                    logger.info("Detail fetch %s/%s", idx, total)
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.2, 0.5))

            # --- Step 3: build records --------------------------------------
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

        csv_path = self.output_dir / "jamesedition_classifieds.csv"
        xlsx_path = self.output_dir / "jamesedition_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)

        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("JamesEdition - Complete: %s records", len(records))
        logger.info("=" * 60)
        return records
