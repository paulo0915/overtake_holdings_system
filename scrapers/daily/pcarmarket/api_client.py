"""PCarMarket async HTTP API client — list pagination and concurrent detail fetching."""

import asyncio
import logging
import random
from typing import Callable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT, get_headers

from .config import PCAR_API_BASE, PCAR_BASE_URL, PCAR_RESULTS_PER_PAGE

logger = logging.getLogger(__name__)

# Transient failures on one page used to silently truncate the scrape. Retry
# 3× with exponential backoff before giving up on a page.
_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


@retry(**_RETRY)
async def _fetch_list_page(
    client: httpx.AsyncClient, *, page: int, limit: int,
) -> dict:
    resp = await client.get(
        f"{PCAR_API_BASE}/results/",
        params={
            "status": "ended", "type": "cars",
            "limit": limit, "page": page,
            "sort_by": "price_high", "make": "all", "series": "all",
        },
        headers=get_headers(referer=f"{PCAR_BASE_URL}/results"),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


class PCarAPIClient:
    """Client for PCarMarket's REST API."""

    def __init__(self, min_price: int, max_pages: int = 0, max_concurrent: int = 4):
        self.min_price = min_price
        self.max_pages = max_pages
        self.max_concurrent = max_concurrent

    async def fetch_listing_list(self, client: httpx.AsyncClient) -> list[dict]:
        """Fetch all ended auction listings with price >= min_price, sorted by price descending."""
        all_listings: list[dict] = []
        page = 1

        while True:
            logger.info(f"Fetching API page {page}...")
            try:
                data = await _fetch_list_page(
                    client, page=page, limit=PCAR_RESULTS_PER_PAGE,
                )
            except Exception as e:
                # All retries exhausted — this is a real, persistent failure, not
                # end-of-results. Log and abort rather than silently truncate.
                logger.error(
                    "PCarMarket list fetch failed on page %s after retries: %s",
                    page, e,
                )
                raise

            results = data.get("results", [])
            if not results:
                logger.info("No more results.")
                break

            logger.info(f"Page {page}: {len(results)} results (total: {data.get('count', 0)})")

            below = False
            for item in results:
                price = item.get("high_bid") or 0
                if price >= self.min_price:
                    all_listings.append(item)
                else:
                    below = True
                    break

            if below:
                logger.info(f"Price below ${self.min_price:,}. Collected {len(all_listings)}.")
                break
            if self.max_pages > 0 and page >= self.max_pages:
                logger.info(f"Max pages ({self.max_pages}). Collected {len(all_listings)}.")
                break
            if not data.get("next"):
                break

            await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.3, 1.0))
            page += 1

        return all_listings

    async def fetch_all_details(
        self,
        client: httpx.AsyncClient,
        listings: list[dict],
        cached: dict[str, dict],
        save_checkpoint: Callable[[list[dict], dict[str, dict]], None],
    ) -> dict[str, dict]:
        """Fetch detail data for all listings concurrently, with checkpoint saving."""
        details = dict(cached)
        to_fetch = [i for i in listings if i.get("slug", "") not in details]

        if not to_fetch:
            logger.info(f"All {len(listings)} details cached.")
            return details

        logger.info(f"Fetching {len(to_fetch)} details ({len(details)} cached)...")
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def fetch_one(item: dict) -> tuple[str, dict]:
            slug = item.get("slug", "")
            detail_slug = item.get("marketplace_listing_slug") or slug
            async with semaphore:
                try:
                    resp = await client.get(
                        f"{PCAR_API_BASE}/auctions/{detail_slug}/",
                        headers=get_headers(referer=f"{PCAR_BASE_URL}/auction/{detail_slug}"),
                        timeout=REQUEST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    # Follow redirect once if detail is missing
                    redirect_slug = data.get("redirect_slug")
                    if redirect_slug and not data.get("description"):
                        resp2 = await client.get(
                            f"{PCAR_API_BASE}/auctions/{redirect_slug}/",
                            headers=get_headers(referer=f"{PCAR_BASE_URL}/auction/{redirect_slug}"),
                            timeout=REQUEST_TIMEOUT,
                        )
                        resp2.raise_for_status()
                        data = resp2.json()

                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.3, 1.0))
                    return (slug, data)
                except Exception as e:
                    logger.warning(f"Detail error for {slug}: {e}")
                    return (slug, {})

        tasks = [fetch_one(item) for item in to_fetch]
        done = 0
        for coro in asyncio.as_completed(tasks):
            slug, data = await coro
            details[slug] = data
            done += 1
            if done % 25 == 0:
                logger.info(f"Fetched {done}/{len(to_fetch)} details (saving checkpoint)")
                save_checkpoint(listings, details)
            elif done == len(to_fetch):
                logger.info(f"Fetched {done}/{len(to_fetch)} details")

        save_checkpoint(listings, details)
        return details
