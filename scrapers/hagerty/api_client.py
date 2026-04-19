"""Hagerty customer GraphQL API + auction HTML fetch."""

import asyncio
import logging
import random
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT, get_headers

from .config import (
    GRAPHQL_ENDPOINT,
    HAGERTY_GRAPHQL_API_KEY,
    HAGERTY_MARKETPLACE_BASE,
    HAGERTY_SITE_ORIGIN,
    RESULTS_PER_PAGE,
)

logger = logging.getLogger(__name__)

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


def _graphql_headers() -> dict[str, str]:
    h = get_headers(accept="application/json", referer=f"{HAGERTY_SITE_ORIGIN}/")
    h["Content-Type"] = "application/json"
    h["Origin"] = HAGERTY_SITE_ORIGIN
    h["x-api-key"] = HAGERTY_GRAPHQL_API_KEY
    return h


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
        headers=_graphql_headers(),
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
