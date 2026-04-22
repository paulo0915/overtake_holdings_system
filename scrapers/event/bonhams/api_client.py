"""HTTP helpers for Bonhams Cars — Typesense search + Next.js HTML pages."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import (
    AUCTIONS_PER_PAGE,
    AUCTIONS_SEARCH_FILTER,
    AUCTIONS_SORT_BY,
    SITE_ORIGIN,
    TYPESENSE_API_KEY,
    TYPESENSE_HOST,
    TYPESENSE_SEARCH_PATH,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_COMMON_HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "en-US,en;q=0.9",
}


async def post_auctions_search(client: httpx.AsyncClient, *, page: int) -> dict[str, Any]:
    """Return the Typesense response for one page of ended Bonhams Cars auctions."""
    body = {
        "searches": [
            {
                "collection": "auctions-search",
                "exclude_fields": "description",
                "filter_by": AUCTIONS_SEARCH_FILTER,
                "facet_by": "auctionType",
                "query_by": "auctionHeading,auctionTitle,departments.name",
                "sort_by": AUCTIONS_SORT_BY,
                "page": page,
                "per_page": AUCTIONS_PER_PAGE,
                "max_facet_values": 10,
                "q": "",
            },
        ],
    }
    url = f"{TYPESENSE_HOST}{TYPESENSE_SEARCH_PATH}?use_cache=true&enable_lazy_filter=true"
    r = await client.post(
        url,
        json=body,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": SITE_ORIGIN,
            "Referer": SITE_ORIGIN + "/",
            "x-typesense-api-key": TYPESENSE_API_KEY,
        },
    )
    r.raise_for_status()
    return r.json()


async def fetch_auction_lots_page_html(
    client: httpx.AsyncClient, *, auction_id: str, slug: str, page: int,
) -> str:
    """GET the auction results page (``__NEXT_DATA__`` contains ``auctionLots``)."""
    url = f"{SITE_ORIGIN}/auction/{auction_id}/{slug}/"
    if page > 1:
        url = f"{url}?page={page}"
    r = await client.get(url, headers={**_COMMON_HTML_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


async def fetch_lot_detail_html(
    client: httpx.AsyncClient, *, auction_id: str, lot_no: str, lot_slug: str,
) -> str:
    """GET a single lot detail page (``__NEXT_DATA__`` contains the full ``lot`` object)."""
    url = f"{SITE_ORIGIN}/auction/{auction_id}/lot/{lot_no}/{lot_slug}/"
    r = await client.get(url, headers={**_COMMON_HTML_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text
