"""Cars & Bids scraper — thin Playwright orchestrator: browser, checkpoint, export."""

import json
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

from shared.base_scraper import BaseScraper
from shared.export import export_to_csv, export_to_excel
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from .api_client import CabAPIClient
from . import record_builder

logger = logging.getLogger(__name__)


class CarsAndBidsScraper(BaseScraper):
    """Cars & Bids scraper using Playwright for Cloudflare bypass."""

    SOURCE_NAME = "Cars & Bids"
    SOURCE_DOMAIN = "carsandbids.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = Path(self.output_dir) / ".cab_checkpoint.json"

    async def run(self) -> list[dict]:
        """Async interface — delegates to run_sync."""
        return self.run_sync()

    def run_sync(self) -> list[dict]:
        """Run the Cars & Bids scraper using sync Playwright."""
        logger.info("=" * 60)
        logger.info("Cars & Bids Scraper - Starting")
        logger.info(f"Min price: ${self.min_price:,}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        checkpoint = self._load_checkpoint()
        cached_listings = checkpoint.get("listings", []) if checkpoint else []
        cached_details = checkpoint.get("details", {}) if checkpoint else {}

        api_client = CabAPIClient(self.min_price, self.max_pages)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            try:
                page = context.new_page()

                if cached_listings:
                    all_listings = cached_listings
                    logger.info(f"Resumed {len(all_listings)} listings from checkpoint")
                else:
                    all_listings = api_client.fetch_auction_list(page)
                    logger.info(f"Fetched {len(all_listings)} listings >= ${self.min_price:,}")
                    if not all_listings:
                        logger.warning("No listings found.")
                        return []

                all_details = self._fetch_all_details(page, api_client, all_listings, cached_details)
            finally:
                browser.close()

        scraped_at = get_scraped_at()
        records = record_builder.build_records(
            all_listings, all_details, self.vehicle_filter, scraped_at
        )
        logger.info(f"Built {len(records)} records")

        csv_path = self.output_dir / "cab_auction_results.csv"
        excel_path = self.output_dir / "cab_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)

        self._delete_checkpoint()

        logger.info("=" * 60)
        logger.info(f"Cars & Bids Scraper - Complete: {len(records)} records")
        logger.info(f"CSV: {csv_path}")
        logger.info(f"Excel: {excel_path}")
        logger.info("=" * 60)

        return records

    # ── Detail fetching loop ────────────────────────────────

    def _fetch_all_details(
        self,
        page,
        api_client: CabAPIClient,
        listings: list[dict],
        cached: dict,
    ) -> dict[str, dict]:
        """Fetch detail API data for all listings, reusing cache when available."""
        details = dict(cached)
        total = len(listings)
        logger.info(f"Fetching details for {total} listings ({len(cached)} cached)...")

        for i, listing in enumerate(listings):
            auction_id = str(listing.get("id", ""))
            if auction_id in details:
                continue

            detail = api_client.fetch_detail(page, auction_id)
            details[auction_id] = detail if detail else {}

            if (i + 1) % 25 == 0:
                logger.info(f"Fetched {i + 1}/{total} details (saving checkpoint)")
                self._save_checkpoint(listings, details)

        logger.info(f"Detail fetch complete: {total} listings")
        return details

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, listings: list[dict], details: dict) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"listings": listings, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
