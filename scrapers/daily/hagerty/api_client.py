"""Hagerty customer GraphQL API + auction HTML fetch."""

import asyncio
import logging
import os
import random
import re
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT, get_headers

from .config import (
    GRAPHQL_ENDPOINT,
    HAGERTY_GRAPHQL_API_KEY_FALLBACK,
    HAGERTY_MARKETPLACE_BASE,
    HAGERTY_SITE_ORIGIN,
    RESULTS_PER_PAGE,
)

logger = logging.getLogger(__name__)

_APP_CHUNK_RE = re.compile(
    r"(/marketplace/_next/static/chunks/pages/_app-[a-fA-F0-9]+\.js)",
)
_DA2_KEY_RE = re.compile(r"da2-[a-z0-9]{20,60}", re.I)

_graphql_api_key: str | None = None
_graphql_api_key_lock = asyncio.Lock()

# GraphQL document copied from the Marketplace search network request.
_MARKETPLACE_SEARCH_DOCUMENT = """query marketplaceSearchPaginated($query: String, $filters: MarketplaceExploreSearchFiltersInput, $sort: MarketplaceExploreSearchSort!, $limit: Int!, $page: Int!) {
  marketplaceSearch {
    search {
      marketplaceExplore(query: $query, filters: $filters, sort: $sort) {
        paginated(limit: $limit, page: $page) {
          totalCount
          pageInfo {
            hasNextPage
            hasPreviousPage
            startCursor
            endCursor
            __typename
          }
          edges {
            cursor
            node {
              ...marketplaceSearchNode
              __typename
            }
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}

fragment marketplaceSearchNode on MarketplaceItemNode {
  __typename
  ...marketplaceSearchListingNode
  ...marketplaceSearchAuctionNode
}

fragment marketplaceSearchListingNode on ListingVehicleSearchNode {
  __typename
  id
  year
  make
  model
  listingLocation: location {
    city
    state
    __typename
  }
  askedPrice {
    amount
    __typename
  }
  listingPhoto: mainPhoto {
    url
    __typename
  }
  endDateTime
  listingStatus
  garageId
}

fragment marketplaceSearchAuctionNode on AuctionVehicleSearchNode {
  __typename
  id
  year
  make
  model
  auctionTitle
  hasReserve
  successfulBidCount
  auctionLocation: location {
    city
    state
    __typename
  }
  auctionPhoto: mainPhoto {
    url
    __typename
  }
  endDateTime
  currentHighestBid {
    amount
    __typename
  }
  hasBeenSold
  hasBeenSoldAfter
  status
  auctionGarageId
}
"""


def _graphql_headers(api_key: str) -> dict[str, str]:
    h = get_headers(accept="application/json", referer=f"{HAGERTY_SITE_ORIGIN}/")
    h["Content-Type"] = "application/json"
    h["Origin"] = HAGERTY_SITE_ORIGIN
    h["x-api-key"] = api_key
    return h


async def _resolve_graphql_api_key(client: httpx.AsyncClient) -> str:
    """
    Resolve the public GraphQL ``x-api-key`` the same way the Marketplace web app does.

    Priority: ``HAGERTY_GRAPHQL_API_KEY`` env (optional override) → key embedded in ``pages/_app-*.js``
    → compiled-in fallback if discovery fails.
    """
    global _graphql_api_key
    if _graphql_api_key:
        return _graphql_api_key
    async with _graphql_api_key_lock:
        if _graphql_api_key:
            return _graphql_api_key

        env_key = (os.getenv("HAGERTY_GRAPHQL_API_KEY") or "").strip()
        if env_key:
            _graphql_api_key = env_key
            logger.info("Using Hagerty GraphQL API key from HAGERTY_GRAPHQL_API_KEY env")
            return _graphql_api_key

        search_url = f"{HAGERTY_MARKETPLACE_BASE}/search"
        try:
            r = await client.get(
                search_url,
                headers=get_headers(
                    accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    referer=f"{HAGERTY_SITE_ORIGIN}/",
                ),
                timeout=REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            html = r.text
        except Exception as e:
            logger.warning("Could not fetch Marketplace HTML for API key discovery: %s", e)
            _graphql_api_key = HAGERTY_GRAPHQL_API_KEY_FALLBACK
            logger.warning("Using compiled Hagerty GraphQL API key fallback")
            return _graphql_api_key

        m = _APP_CHUNK_RE.search(html)
        if not m:
            logger.warning("No pages/_app-*.js chunk URL found in Marketplace HTML; using fallback key")
            _graphql_api_key = HAGERTY_GRAPHQL_API_KEY_FALLBACK
            return _graphql_api_key

        app_js_url = f"{HAGERTY_SITE_ORIGIN}{m.group(1)}"
        try:
            jr = await client.get(
                app_js_url,
                headers=get_headers(accept="*/*", referer=search_url),
                timeout=REQUEST_TIMEOUT,
            )
            jr.raise_for_status()
            km = _DA2_KEY_RE.search(jr.text)
        except Exception as e:
            logger.warning("Could not fetch or parse _app chunk for API key: %s", e)
            _graphql_api_key = HAGERTY_GRAPHQL_API_KEY_FALLBACK
            return _graphql_api_key

        if not km:
            logger.warning("No da2-* API key pattern in _app chunk; using fallback")
            _graphql_api_key = HAGERTY_GRAPHQL_API_KEY_FALLBACK
            return _graphql_api_key

        _graphql_api_key = km.group(0)
        logger.info("Discovered Hagerty GraphQL x-api-key from Marketplace client bundle")
        return _graphql_api_key


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
)
async def marketplace_search_page(
    client: httpx.AsyncClient,
    page: int,
    limit: int = RESULTS_PER_PAGE,
) -> dict[str, Any]:
    """One page of marketplace search (ended auctions, price descending), with 3-attempt retry."""
    api_key = await _resolve_graphql_api_key(client)
    payload = {
        "operationName": "marketplaceSearchPaginated",
        "variables": {
            "query": "",
            "sort": "PRICE_DESC",
            "filters": {"type": "AUCTIONS", "forSale": False},
            "limit": limit,
            "page": page,
        },
        "query": _MARKETPLACE_SEARCH_DOCUMENT,
    }
    resp = await client.post(
        GRAPHQL_ENDPOINT,
        json=payload,
        headers=_graphql_headers(api_key),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        # GraphQL errors on partial responses — log but return so caller can decide.
        logger.warning(f"GraphQL errors on page {page}: {data['errors']}")
    return data


async def fetch_auction_html(client: httpx.AsyncClient, ymm_slug: str, auction_id: str) -> str | None:
    """GET auction detail HTML (embeds __NEXT_DATA__ with full vehicle + auction payload)."""
    url = f"{HAGERTY_MARKETPLACE_BASE}/auction/{ymm_slug}/{auction_id}"
    try:
        resp = await client.get(
            url,
            headers=get_headers(
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                referer=f"{HAGERTY_MARKETPLACE_BASE}/search",
            ),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.2, 0.8))
        return resp.text
    except Exception as e:
        logger.warning(f"Auction page fetch failed {url}: {e}")
        return None
