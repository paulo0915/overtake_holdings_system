"""HTTP — Typesense multi_search (httpx) + listing pages (curl_cffi / Cloudflare)."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import (
    LISTING_BASE,
    MULTI_SEARCH_URL,
    RESULTS_REFERER,
    TYPESENSE_API_KEY,
)

logger = logging.getLogger(__name__)
_TIMEOUT = float(REQUEST_TIMEOUT)


def _listing_headers() -> dict[str, str]:
    """Document-style GET — parallel anonymous hits often get 403 without these."""
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "referer": RESULTS_REFERER,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
    }


class CollectingCarsApiError(RuntimeError):
    """Typesense or unexpected HTTP failure."""


def _typesense_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://collectingcars.com",
        "referer": RESULTS_REFERER,
    }


def _search_body(*, page: int, per_page: int) -> dict[str, Any]:
    """Single search: sold cars only, newest first, no embedding vectors."""
    return {
        "searches": [
            {
                "collection": "production_cars",
                "q": "*",
                "query_by": "title,productMake,vehicleMake,productYear",
                "filter_by": "listingStage:sold && lotType:car",
                "sort_by": "tsSoldUTC:desc",
                "per_page": per_page,
                "page": page,
                "exclude_fields": "embedding",
            }
        ],
    }


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type((httpx.HTTPError, CollectingCarsApiError)),
)
async def fetch_typesense_page(
    client: httpx.AsyncClient,
    *,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    """POST one page of sold-car hits from Typesense."""
    r = await client.post(
        MULTI_SEARCH_URL,
        params={"x-typesense-api-key": TYPESENSE_API_KEY},
        json=_search_body(page=page, per_page=per_page),
        headers=_typesense_headers(),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise CollectingCarsApiError(f"Typesense HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    results = data.get("results") or []
    if not results:
        raise CollectingCarsApiError("Typesense returned no results[]")
    first = results[0]
    if first.get("code") or first.get("error"):
        raise CollectingCarsApiError(f"Typesense error: {first!r}"[:500])
    return first


async def fetch_listing_html(session: AsyncSession, slug: str) -> str | None:
    """GET listing HTML; retry on 429/403 (Cloudflare) with backoff."""
    url = f"{LISTING_BASE}/{slug}"
    headers = _listing_headers()
    last_status: int | None = None
    for attempt in range(5):
        try:
            r = await session.get(url, headers=headers, timeout=_TIMEOUT)
        except RequestsError as e:
            logger.warning("Listing %s request error: %s", slug, e)
            if attempt < 4:
                await asyncio.sleep(2.0 * (attempt + 1) + random.uniform(0, 0.5))
                continue
            return None
        last_status = r.status_code
        if r.status_code == 200:
            return r.text
        if r.status_code in (403, 429) and attempt < 4:
            delay = 3.0 * (2**attempt) + random.uniform(0.5, 2.0)
            logger.debug("Listing %s HTTP %s — sleeping %.1fs then retry", slug, r.status_code, delay)
            await asyncio.sleep(delay)
            continue
        break
    logger.warning("Listing %s HTTP %s", slug, last_status)
    return None
