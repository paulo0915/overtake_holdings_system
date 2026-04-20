"""BaT scraper — thin async orchestrator: warmup, checkpoint, page fetching, export."""

import asyncio
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor

import httpx

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from .api_client import BaTAPIClient
from .config import get_page_headers
from .listing_parser import ListingParser
from .record_builder import build_records

logger = logging.getLogger(__name__)


class BaTScraper(BaseScraper):
    """Bring a Trailer scraper."""

    SOURCE_NAME = "Bring a Trailer"
    SOURCE_DOMAIN = "bringatrailer.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parser = ListingParser()
        self._checkpoint_path = self.output_dir / ".checkpoint.json"

    async def run(self) -> list[dict]:
        """Run the full BaT scraping pipeline."""
        logger.info("=" * 60)
        logger.info("BaT Scraper - Starting")
        logger.info(f"Min price: ${self.min_price:,}")
        logger.info(f"Concurrency: {self.max_concurrent}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(http2=True, follow_redirects=True) as client:
            await self._warmup_session(client)

            # Step 1: API pagination (or resume from checkpoint)
            checkpoint = self._load_checkpoint()
            if checkpoint and checkpoint.get("api_listings"):
                raw_listings = checkpoint["api_listings"]
                logger.info(f"Resumed {len(raw_listings)} listings from checkpoint")
            else:
                api_client = BaTAPIClient(client, self.min_price)
                raw_listings = await api_client.fetch_all_above_threshold(self.max_pages)
                logger.info(f"API returned {len(raw_listings)} listings >= ${self.min_price:,}")

            if not raw_listings:
                logger.warning("No listings found. Exiting.")
                return []

            # Step 2: Fetch listing HTML pages (with resume)
            already_parsed = checkpoint.get("parsed_details", {}) if checkpoint else {}
            if already_parsed:
                logger.info(f"Checkpoint has {len(already_parsed)} already-parsed listings")

            semaphore = asyncio.Semaphore(self.max_concurrent)
            details = await self._fetch_listing_details(
                client, raw_listings, semaphore, already_parsed
            )

            # Step 3: Normalize, filter, assemble records
            fx_converter = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await build_records(
                raw_listings, details, fx_converter, self.vehicle_filter, scraped_at
            )
            logger.info(f"Built {len(records)} final records")

            # Step 4: Export CSV/Excel
            csv_path = self.output_dir / "bat_auction_results.csv"
            excel_path = self.output_dir / "bat_auction_results.xlsx"
            export_to_csv(records, csv_path, AUCTION_COLUMNS)
            export_to_excel(records, excel_path, AUCTION_COLUMNS)

            self._delete_checkpoint()

            logger.info("=" * 60)
            logger.info("BaT Scraper - Complete")
            logger.info(f"Total records: {len(records)}")
            logger.info(f"CSV: {csv_path}")
            logger.info(f"Excel: {excel_path}")
            logger.info("=" * 60)

            return records

    # ── Session warmup ──────────────────────────────────────

    async def _warmup_session(self, client: httpx.AsyncClient) -> None:
        logger.info("Warming up session...")
        try:
            resp = await client.get(
                "https://bringatrailer.com/",
                headers=get_page_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            logger.info(f"Session warmup done (status={resp.status_code}, cookies={len(client.cookies)})")
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning(f"Session warmup failed: {e}")

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load checkpoint: {e}")
            return None

    def _save_checkpoint(self, api_listings: list[dict], parsed_details: dict[str, dict]) -> None:
        self._checkpoint_path.write_text(
            json.dumps(
                {"api_listings": api_listings, "parsed_details": parsed_details},
                ensure_ascii=False, default=str,
            ),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")

    # ── Listing HTML page fetching ──────────────────────────

    async def _fetch_single_listing(self, client: httpx.AsyncClient, url: str, semaphore) -> str | None:
        async with semaphore:
            for attempt in range(3):
                try:
                    resp = await client.get(url, headers=get_page_headers(), timeout=REQUEST_TIMEOUT)
                    if resp.status_code in (429, 403):
                        wait = 15 * (attempt + 1) + random.uniform(0, 5)
                        logger.warning(f"{resp.status_code} on {url}, waiting {wait:.0f}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    html = resp.text
                    if any(s in html[:5000] for s in
                           ("cf-browser-verification", "cf_chl_opt", "Just a moment...")):
                        await asyncio.sleep(15 * (attempt + 1) + random.uniform(0, 5))
                        continue
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.5, 2.0))
                    return html
                except (httpx.HTTPStatusError, httpx.ConnectError,
                        httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    if attempt == 2:
                        logger.warning(f"Failed {url} after 3 attempts: {e}")
                        return None
                    await asyncio.sleep(3 * (attempt + 1) + random.uniform(0, 2))
        return None

    async def _fetch_listing_details(
        self,
        client: httpx.AsyncClient,
        listings: list[dict],
        semaphore,
        already_parsed: dict[str, dict],
    ) -> dict[str, dict]:
        results = dict(already_parsed)
        to_fetch = [i for i in listings if i.get("url", "") not in results]

        if not to_fetch:
            logger.info("All listings in checkpoint. Skipping fetch.")
            return results

        logger.info(f"Fetching {len(to_fetch)} pages ({len(already_parsed)} cached)...")
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=self.max_concurrent)

        async def fetch_and_parse(item: dict) -> tuple[str, dict]:
            url = item.get("url", "")
            try:
                html = await self._fetch_single_listing(client, url, semaphore)
                if html:
                    parsed = await loop.run_in_executor(executor, self.parser.parse, html, url)
                    return (url, parsed)
            except Exception as e:
                logger.warning(f"Error on {url}: {e}")
            return (url, {})

        tasks = [fetch_and_parse(i) for i in to_fetch]
        done = 0
        for coro in asyncio.as_completed(tasks):
            url, parsed = await coro
            results[url] = parsed
            done += 1
            if done % 50 == 0:
                logger.info(f"Fetched {done}/{len(to_fetch)} (saving checkpoint)")
                self._save_checkpoint(listings, results)
            elif done == len(to_fetch):
                logger.info(f"Fetched {done}/{len(to_fetch)}")

        executor.shutdown(wait=True)
        self._save_checkpoint(listings, results)
        return results
