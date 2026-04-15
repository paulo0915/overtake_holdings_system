"""BaT API client - paginate through auction results."""

import asyncio
import logging
import random
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import (
    BAT_API_BASE,
    BAT_RESULTS_PER_PAGE,
    BAT_SORT,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    MIN_PRICE_USD,
    get_api_headers,
)

logger = logging.getLogger(__name__)


class BaTAPIClient:
    """Client for BaT's listings-filter API."""

    def __init__(self, client: httpx.AsyncClient, min_price: int = MIN_PRICE_USD):
        self.client = client
        self.min_price = min_price

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
    )
    async def _fetch_page(self, page: int) -> dict[str, Any]:
        """Fetch a single page of results from the API."""
        params = {
            "page": page,
            "per_page": BAT_RESULTS_PER_PAGE,
            "get_items": 1,
            "get_stats": 0,
            "sort": BAT_SORT,
        }

        resp = await self.client.get(
            BAT_API_BASE,
            params=params,
            headers=get_api_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_all_above_threshold(self, max_pages: int = 0) -> list[dict[str, Any]]:
        """
        Fetch all listings with current_bid >= min_price.

        Sorted by bid descending — stops when bids drop below threshold.
        """
        all_items = []
        page = 1

        while True:
            logger.info(f"Fetching API page {page}...")
            data = await self._fetch_page(page)

            items = data.get("items", [])
            if not items:
                logger.info("No more items returned. Done.")
                break

            page_total = data.get("pages_total") or float("inf")
            items_total = data.get("items_total", 0)
            logger.info(
                f"Page {page}/{page_total} — {len(items)} items "
                f"(total available: {items_total})"
            )

            below_threshold = False
            for item in items:
                bid = int(item.get("current_bid") or 0)
                if bid >= self.min_price:
                    all_items.append(item)
                else:
                    below_threshold = True
                    break

            if below_threshold:
                logger.info(
                    f"Bid dropped below ${self.min_price:,} on page {page}. "
                    f"Collected {len(all_items)} listings total."
                )
                break

            if page >= page_total:
                logger.info("Reached last page.")
                break

            if max_pages > 0 and page >= max_pages:
                logger.info(
                    f"Reached max_pages limit ({max_pages}). "
                    f"Collected {len(all_items)} listings total."
                )
                break

            await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.3, 1.0))
            page += 1

        return all_items
