"""PCarMarket scraper — thin async orchestrator: checkpoint, API calls, export."""

import json
import logging

import httpx

from shared.base_scraper import BaseScraper
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from .api_client import PCarAPIClient
from . import record_builder

logger = logging.getLogger(__name__)


class PCarMarketScraper(BaseScraper):
    """PCarMarket scraper — async httpx, direct API access."""

    SOURCE_NAME = "PCarMarket"
    SOURCE_DOMAIN = "pcarmarket.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = self.output_dir / ".pcar_checkpoint.json"

    async def run(self) -> list[dict]:
        """Run the PCarMarket scraping pipeline."""
        logger.info("=" * 60)
        logger.info("PCarMarket Scraper - Starting")
        logger.info(f"Min price: ${self.min_price:,}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        checkpoint = self._load_checkpoint()
        cached_listings = checkpoint.get("listings", []) if checkpoint else []
        cached_details = checkpoint.get("details", {}) if checkpoint else {}

        api_client = PCarAPIClient(self.min_price, self.max_pages, self.max_concurrent)

        async with httpx.AsyncClient(follow_redirects=True) as client:
            if cached_listings:
                listings = cached_listings
                logger.info(f"Resumed {len(listings)} listings from checkpoint")
            else:
                listings = await api_client.fetch_listing_list(client)
                logger.info(f"Collected {len(listings)} listings >= ${self.min_price:,}")

            if not listings:
                logger.warning("No listings found.")
                return []

            details = await api_client.fetch_all_details(
                client, listings, cached_details, self._save_checkpoint,
            )

            fx_converter = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                listings, details, fx_converter, self.vehicle_filter, scraped_at,
            )
            logger.info(f"Built {len(records)} records")

            csv_path = self.output_dir / "pcar_auction_results.csv"
            excel_path = self.output_dir / "pcar_auction_results.xlsx"
            export_to_csv(records, csv_path, AUCTION_COLUMNS)
            export_to_excel(records, excel_path, AUCTION_COLUMNS)
            self._delete_checkpoint()

            logger.info("=" * 60)
            logger.info(f"PCarMarket Scraper - Complete: {len(records)} records")
            logger.info("=" * 60)

        return records

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, listings: list[dict], details: dict[str, dict]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"listings": listings, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
