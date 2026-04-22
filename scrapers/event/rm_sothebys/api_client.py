"""HTTP helpers for RM Sotheby's HTML + SearchLots JSON."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import DEFAULT_PAGE_SIZE, SEARCH_LOTS_BODY_TEMPLATE, SEARCH_LOTS_PATH, SITE_ORIGIN

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _lots_referer(auction_slug_lower: str) -> str:
    return f"{SITE_ORIGIN}/auctions/{auction_slug_lower}/lots/"


async def fetch_results_year_html(client: httpx.AsyncClient, year: int) -> str:
    url = f"{SITE_ORIGIN}/results/{year}"
    r = await client.get(url, headers={**_DEFAULT_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


async def fetch_upcoming_html(client: httpx.AsyncClient) -> str:
    url = f"{SITE_ORIGIN}/upcoming/"
    r = await client.get(url, headers={**_DEFAULT_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


async def fetch_auction_lots_page_html(client: httpx.AsyncClient, slug: str) -> str:
    """GET ``/auctions/{slug}/lots/`` — landing page with title + date."""
    url = f"{SITE_ORIGIN}/auctions/{slug.lower()}/lots/"
    r = await client.get(url, headers={**_DEFAULT_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


async def post_search_lots(
    client: httpx.AsyncClient,
    *,
    auction_code_upper: str,
    auction_slug_lower: str,
    page: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    body = dict(SEARCH_LOTS_BODY_TEMPLATE)
    body["Auction"] = auction_code_upper
    url = f"{SITE_ORIGIN}{SEARCH_LOTS_PATH}?page={page}&pageSize={page_size}"
    r = await client.post(
        url,
        json=body,
        headers={
            **_DEFAULT_HEADERS,
            "Origin": SITE_ORIGIN,
            "Referer": _lots_referer(auction_slug_lower),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
        },
    )
    r.raise_for_status()
    return r.json()


async def fetch_lot_detail_html(client: httpx.AsyncClient, listing_path: str) -> str:
    """``listing_path`` starts with ``/auctions/...``."""
    url = f"{SITE_ORIGIN}{listing_path}" if listing_path.startswith("/") else f"{SITE_ORIGIN}/{listing_path}"
    r = await client.get(
        url,
        headers={**_DEFAULT_HEADERS, "Referer": SITE_ORIGIN + "/"},
    )
    r.raise_for_status()
    return r.text
