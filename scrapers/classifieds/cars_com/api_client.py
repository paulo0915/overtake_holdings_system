"""HTTP helpers for Cars.com — GraphQL search + listing detail."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    DEFAULT_PAGE_SIZE,
    DETAIL_QUERY,
    GRAPHQL_URL,
    LIST_QUERY,
    build_headers,
)

logger = logging.getLogger(__name__)

# Per-process trip-id — Cars.com treats this as a session cookie so reusing
# one value across a run looks more like a real browser than rotating it.
_TRIP_ID = str(uuid.uuid4())


_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


def _check_gql_errors(payload: dict[str, Any], op: str) -> None:
    """Raise on GraphQL-level errors (HTTP 200 + ``errors`` array)."""
    errs = payload.get("errors")
    if errs:
        # Surface the first message — usually enough to diagnose.
        msg = errs[0].get("message") if isinstance(errs[0], dict) else str(errs[0])
        raise RuntimeError(f"Cars.com {op} GraphQL error: {msg}")


@retry(**_RETRY)
async def post_search_list(
    client: httpx.AsyncClient,
    *,
    page: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Call ``SearchResultsPageSearch``. Returns ``{"metadata", "results"}``.

    Returns the whole US inventory — ``srpSearch`` defaults to "all listings"
    when no filters are supplied. Sorted ``LIST_PRICE_DESC`` (baked into the
    query text as an enum literal) so ``--min-price`` short-circuits
    pagination once prices drop below the floor.
    """
    body = {
        "operationName": "SearchResultsPageSearch",
        "variables": {
            "filters": [],
            "page": page,
            "pageSize": page_size,
        },
        "query": LIST_QUERY,
    }
    r = await client.post(GRAPHQL_URL, json=body, headers=build_headers(_TRIP_ID))
    r.raise_for_status()
    payload = r.json()
    _check_gql_errors(payload, "SearchResultsPageSearch")
    return (payload.get("data") or {}).get("srpSearch") or {}


@retry(**_RETRY)
async def post_listing_details(
    client: httpx.AsyncClient,
    *,
    listing_id: str,
) -> dict[str, Any] | None:
    """Call ``listingDetails(listingId:)``. Returns the VDP payload or ``None``
    if the listing has been pulled between the list fetch and this call.
    """
    body = {
        "operationName": "ListingDetails",
        "variables": {"listingId": listing_id},
        "query": DETAIL_QUERY,
    }
    r = await client.post(GRAPHQL_URL, json=body, headers=build_headers(_TRIP_ID))
    r.raise_for_status()
    payload = r.json()
    _check_gql_errors(payload, "ListingDetails")
    return (payload.get("data") or {}).get("listingDetails")
