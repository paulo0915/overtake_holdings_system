"""Hagerty Marketplace — GraphQL search + auction HTML (__NEXT_DATA__) pipeline."""

import asyncio
import json
import logging

import httpx

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from .api_client import fetch_auction_html, marketplace_search_page
from .config import RESULTS_PER_PAGE
from .listing_parser import extract_auction_edges, parse_auction_html, parse_search_summary
from . import record_builder

logger = logging.getLogger(__name__)


class HagertyMarketplaceScraper(BaseScraper):
    """Ended Hagerty Marketplace auctions — sorted by price descending."""

    SOURCE_NAME = "Hagerty Marketplace"
    SOURCE_DOMAIN = "hagerty.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = self.output_dir / ".hagerty_checkpoint.json"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("Hagerty Marketplace Scraper - Starting")
        logger.info(f"Min price (USD): ${self.min_price:,}")
        logger.info(f"Concurrency: {self.max_concurrent}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max search pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        checkpoint = self._load_checkpoint()
        cached_summaries = checkpoint.get("summaries") if checkpoint else None
        cached_details = checkpoint.get("details") if checkpoint else {}

        async with httpx.AsyncClient(follow_redirects=True) as client:
            if cached_summaries:
                summaries = cached_summaries
                logger.info(f"Resumed {len(summaries)} listings from checkpoint")
            else:
                summaries = await self._collect_search_summaries(client)
                logger.info(f"Search API collected {len(summaries)} auctions >= ${self.min_price:,}")

            if not summaries:
                logger.warning("No auctions matched criteria.")
                return []

            details = await self._fetch_all_details(client, summaries, cached_details)
            fx = FXConverter(client)
            scraped_at = get_scraped_at()
            records = await record_builder.build_records(
                summaries, details, fx, self.vehicle_filter, scraped_at,
            )
            logger.info(f"Built {len(records)} records")

            csv_path = self.output_dir / "hagerty_auction_results.csv"
            excel_path = self.output_dir / "hagerty_auction_results.xlsx"
            export_to_csv(records, csv_path, AUCTION_COLUMNS)
            export_to_excel(records, excel_path, AUCTION_COLUMNS)

        self._delete_checkpoint()
        logger.info("=" * 60)
        logger.info(f"Hagerty Marketplace - Complete: {len(records)} records")
        logger.info("=" * 60)
        return records

    async def _collect_search_summaries(self, client: httpx.AsyncClient) -> list[dict]:
        summaries: list[dict] = []
        page = 1

        while True:
            logger.info(f"GraphQL search page {page}...")
            try:
                data = await marketplace_search_page(client, page, RESULTS_PER_PAGE)
            except Exception as e:
                logger.warning(f"Search page {page} failed: {e}")
                break

            auctions, page_info = extract_auction_edges(data)
            if not auctions:
                logger.info("No auction edges on this page — done.")
                break

            stop = False
            for node in auctions:
                s = parse_search_summary(node)
                hb = s.get("high_bid_usd")
                if hb is None:
                    continue
                if hb < self.min_price:
                    stop = True
                    break
                summaries.append(s)

            if stop:
                logger.info(f"Reached listings below ${self.min_price:,} — stopping pagination.")
                break

            if not page_info.get("hasNextPage"):
                logger.info("Search reports no further pages.")
                break

            if self.max_pages > 0 and page >= self.max_pages:
                logger.info(f"Hit max_pages={self.max_pages}.")
                break

            await asyncio.sleep(REQUEST_DELAY_SECONDS)
            page += 1

        return summaries

    async def _fetch_all_details(
        self,
        client: httpx.AsyncClient,
        summaries: list[dict],
        cached: dict,
    ) -> dict[str, dict | None]:
        """Concurrently fetch + parse auction HTML; merge into details dict keyed by auction id."""
        details: dict[str, dict | None] = {str(k): v for k, v in cached.items()}
        to_fetch = [s for s in summaries if str(s["id"]) not in details or details[str(s["id"])] is None]

        if not to_fetch:
            logger.info("All auction details already cached.")
            return details

        logger.info(f"Fetching {len(to_fetch)} auction detail pages...")
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def one(s: dict) -> tuple[str, dict | None]:
            aid = str(s["id"])
            slug = s.get("slug") or ""
            async with semaphore:
                html = await fetch_auction_html(client, slug, aid)
                return aid, parse_auction_html(html)

        # Use as_completed so results are stored and checkpointed as they arrive,
        # not only after all concurrent fetches finish (matches BaT/PCar pattern).
        tasks = [one(s) for s in to_fetch]
        done = 0
        for coro in asyncio.as_completed(tasks):
            aid, parsed = await coro
            details[aid] = parsed
            done += 1
            if done % 25 == 0:
                logger.info(f"Parsed {done}/{len(to_fetch)} detail pages (checkpoint)")
                self._save_checkpoint(summaries, details)

        logger.info(f"Parsed {done}/{len(to_fetch)} detail pages")
        self._save_checkpoint(summaries, details)
        return details

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Checkpoint load failed: {e}")
            return None

    def _save_checkpoint(self, summaries: list[dict], details: dict[str, dict | None]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"summaries": summaries, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
