"""C&B Playwright API client — list pagination and per-auction detail fetching."""

import logging
import random

from playwright.sync_api import Page

from .config import CAB_BASE_URL

logger = logging.getLogger(__name__)


class CabAPIClient:
    """Fetch C&B auction data via Playwright response interception."""

    def __init__(self, min_price: int, max_pages: int = 0):
        self.min_price = min_price
        self.max_pages = max_pages

    def fetch_auction_list(self, page: Page) -> list[dict]:
        """
        Navigate past-auctions pages sorted by highest price.

        Stops when price drops below min_price or max_pages is reached.
        """
        all_listings: list[dict] = []
        page_num = 0
        captured: list[dict] = []

        def on_response(response):
            if "/v2/autos/auctions" in response.url and "status=closed" in response.url:
                try:
                    captured.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        logger.info("Loading past auctions (sorted by highest price)...")
        try:
            page.goto(
                f"{CAB_BASE_URL}/past-auctions/?sort=highest_price",
                timeout=90000,
                wait_until="domcontentloaded",
            )
        except Exception:
            pass
        page.wait_for_timeout(12000)

        if not captured:
            logger.error("No API data captured on first page. Cloudflare may have blocked.")
            return []

        # Process first page
        data = captured[-1]
        total = data.get("total", 0)
        auctions = data.get("auctions", [])
        below = self._collect_above_threshold(auctions, all_listings)
        page_num = 1
        logger.info(f"Page 1: {len(auctions)} auctions (total: {total}), kept: {len(all_listings)}")

        if below:
            logger.info(
                f"Price dropped below ${self.min_price:,} on page {page_num}. "
                f"Collected {len(all_listings)} listings."
            )
            return all_listings

        # Paginate
        while True:
            if self.max_pages > 0 and page_num >= self.max_pages:
                logger.info(
                    f"Reached max_pages limit ({self.max_pages}). "
                    f"Collected {len(all_listings)} listings."
                )
                break

            page_num += 1
            captured.clear()

            logger.info(f"Loading page {page_num}...")
            try:
                page.goto(
                    f"{CAB_BASE_URL}/past-auctions/?sort=highest_price&page={page_num}",
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception:
                pass
            page.wait_for_timeout(8000 + random.randint(0, 3000))

            if not captured:
                logger.warning(f"No API data on page {page_num}, stopping.")
                break

            data = captured[-1]
            auctions = data.get("auctions", [])
            if not auctions:
                logger.info("No more auctions.")
                break

            below = self._collect_above_threshold(auctions, all_listings)
            logger.info(f"Page {page_num}: {len(auctions)} auctions, kept: {len(all_listings)}")

            if below:
                logger.info(
                    f"Price dropped below ${self.min_price:,} on page {page_num}. "
                    f"Collected {len(all_listings)} listings."
                )
                break

        return all_listings

    def fetch_detail(self, page: Page, auction_id: str) -> dict | None:
        """Fetch the detail API response for a single auction."""
        detail_data: list[dict] = []

        def on_response(response):
            if f"/v2/autos/auctions/{auction_id}" in response.url:
                try:
                    detail_data.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        url = f"{CAB_BASE_URL}/auctions/{auction_id}/"
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(5000 + random.randint(0, 2000))

        page.remove_listener("response", on_response)

        if detail_data:
            return detail_data[-1]

        logger.warning(f"No detail data captured for auction {auction_id}")
        return None

    def _collect_above_threshold(self, auctions: list[dict], result: list[dict]) -> bool:
        """
        Append auctions above min_price to result.

        Returns True when price drops below threshold (caller should stop paginating).
        Since results are sorted by price descending, the first sub-threshold entry
        signals that all remaining will also be below.
        """
        for a in auctions:
            price = a.get("sale_amount") or a.get("current_bid") or 0
            if price >= self.min_price:
                result.append(a)
            else:
                return True
        return False
