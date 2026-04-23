"""HTTP helpers for CarGurus — SRP pagination + detail loader."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    DEFAULT_PAGE_SIZE,
    DETAIL_DATA_ROUTE,
    SEARCH_DATA_ROUTE,
    SEARCH_PATH,
    SITE_ORIGIN,
    srp_variation,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

_SRP_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}{SEARCH_PATH}",
}

_DETAIL_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_ORIGIN}{SEARCH_PATH}",
}

# Pagination form body — the browser sends exactly these three fields; the
# server paginates based on the ``page`` query param, not this body, but the
# body is still required to avoid a 400.
_SRP_BODY = "_action=pagination&seenSponsored=W10%3D&paginationSubtype=next"

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


@retry(**_RETRY)
async def post_search_page(
    client: httpx.AsyncClient,
    *,
    mode: str,
    page: int,
    distance: int = 100,
) -> dict[str, Any]:
    """Fetch one SRP page. Returns the raw ``{"tiles": [...]}`` JSON body.

    ``mode`` picks used vs new (srpVariation). ``distance=100`` mirrors the
    site default — tighter radii would hide most of the luxury inventory.
    """
    params = {
        "srpVariation": srp_variation(mode),
        "distance": str(distance),
        "sortDirection": "DESC",
        "sortType": "PRICE",
        "page": str(page),
        "_data": SEARCH_DATA_ROUTE,
    }
    r = await client.post(
        f"{SITE_ORIGIN}{SEARCH_PATH}",
        params=params,
        headers=_SRP_HEADERS,
        content=_SRP_BODY,
    )
    r.raise_for_status()
    return r.json()


@retry(**_RETRY)
async def fetch_detail_payload(
    client: httpx.AsyncClient,
    *,
    listing_id: int | str,
) -> dict[str, Any] | None:
    """Fetch the detail-page Remix loader payload.

    Response is ``text/remix-deferred``: the first newline-terminated chunk
    is the main JSON, later chunks are deferred sub-resolvers that we don't
    need (recommendations, ads). Parse only chunk 0.

    Returns the decoded root object or ``None`` on a non-200 / malformed
    body (listing pulled between list + detail fetch).
    """
    params = {"_data": DETAIL_DATA_ROUTE}
    r = await client.get(
        f"{SITE_ORIGIN}/details/{listing_id}",
        params=params,
        headers=_DETAIL_HEADERS,
    )
    if r.status_code != 200:
        return None
    first_chunk = r.text.split("\n", 1)[0]
    if not first_chunk:
        return None
    try:
        return json.loads(first_chunk)
    except json.JSONDecodeError as e:
        logger.debug("detail chunk parse failed for %s: %s", listing_id, e)
        return None
