"""HTTP client — Carbuckle Typesense search API + Firestore REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import BASE_URL, COLLECTION, FIREBASE_PROJECT, PER_PAGE, TYPESENSE_API_KEY, TYPESENSE_HOST

logger = logging.getLogger(__name__)

_SEARCH_BASE = f"https://{TYPESENSE_HOST}/collections/{COLLECTION}/documents/search"
_HEADERS = {
    "X-TYPESENSE-API-KEY": TYPESENSE_API_KEY,
    "origin": "https://www.carbuckle.com",
    "referer": "https://www.carbuckle.com/",
}
_FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
    "/databases/(default)/documents/listings"
)

# Only the fields we actually need — omits facets to keep responses lean
_QUERY_PARAMS = {
    "q": "*",
    "query_by": "title,make,model",
    "sort_by": "dateEnd:desc",
    "filter_by": "isSold:[true]",
    "per_page": str(PER_PAGE),
    "exclude_fields": "facet_counts",
}


class CarbuckleApiError(RuntimeError):
    """Raised when the Typesense API call fails."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
)
async def fetch_firestore_doc(client: httpx.AsyncClient, listing_id: str) -> dict[str, Any] | None:
    """Fetch a single listing document from Firestore REST API (publicly readable).

    Returns the raw Firestore document dict, or ``None`` on any error.
    """
    url = f"{_FIRESTORE_BASE}/{listing_id}"
    try:
        r = await client.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
        logger.debug("Firestore %s → HTTP %s", listing_id, r.status_code)
    except Exception as e:
        logger.debug("Firestore fetch failed for %s: %s", listing_id, e)
    return None


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
)
async def fetch_listing_html(client: httpx.AsyncClient, path: str) -> str | None:
    """Fetch the rendered HTML of a Carbuckle listing page (VIN fallback)."""
    url = f"{BASE_URL}/listing/{path.lstrip('/')}"
    try:
        r = await client.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.text
        logger.debug("Carbuckle listing HTML %s → HTTP %s", path, r.status_code)
    except Exception as e:
        logger.debug("Carbuckle listing HTML fetch failed for %s: %s", path, e)
    return None


async def fetch_page(client: httpx.AsyncClient, page: int) -> dict[str, Any]:
    """Fetch one page of sold listings from the Typesense search API."""
    params = {**_QUERY_PARAMS, "page": str(page)}
    r = await client.get(_SEARCH_BASE, params=params, headers=_HEADERS, timeout=30)
    if r.status_code != 200:
        raise CarbuckleApiError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()
