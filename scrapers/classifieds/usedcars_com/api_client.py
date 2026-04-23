"""HTTP helpers for UsedCars.com (Dealix API)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    API_ORIGIN,
    DEFAULT_PAGE_SIZE,
    DEFAULT_RADIUS_MILES,
    DEFAULT_ZIPCODE,
    DETAIL_PATH,
    LIST_PATH,
    SITE_ORIGIN,
    SOURCE_ID,
    SUBSCRIPTION_KEY,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}/",
    # Azure APIM subscription key — required; without it the endpoint 401s.
    "se-aw": SUBSCRIPTION_KEY,
}

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


@retry(**_RETRY)
async def fetch_list_page(
    client: httpx.AsyncClient,
    *,
    page: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    zipcode: str = DEFAULT_ZIPCODE,
    radius: int = DEFAULT_RADIUS_MILES,
    price_max: int = 999_999,
) -> dict[str, Any]:
    """Fetch one inventory page sorted ``highest_price`` DESC.

    Returns the raw body ``{carsForSale, dealers, inventoryCount, ...}``.
    Sorting ``highest_price`` pairs with ``--min-price`` to stop pagination
    once the listing price drops below the floor.
    """
    params = {
        "SourceID": SOURCE_ID,
        "Radius": str(radius),
        "Zipcode": zipcode,
        "SortBy": "highest_price",
        "Page.Current": str(page),
        "Page.Size": str(page_size),
        "Price.Max": str(price_max),
        "includeResultFilters": "true",
        "InventorySource": "both",
    }
    r = await client.get(
        f"{API_ORIGIN}{LIST_PATH}", params=params, headers=_HEADERS,
    )
    r.raise_for_status()
    return r.json()


@retry(**_RETRY)
async def fetch_detail(
    client: httpx.AsyncClient,
    *,
    used_car_id: int | str,
    zipcode: str = DEFAULT_ZIPCODE,
) -> dict[str, Any] | None:
    """Fetch the per-VIN detail payload. Returns ``None`` if the listing is
    gone (404) — Dealix occasionally pulls rows between list + detail calls.
    """
    params = {
        "usedCarId": str(used_car_id),
        "sourceId": SOURCE_ID,
        "zipCode": zipcode,
    }
    r = await client.get(
        f"{API_ORIGIN}{DETAIL_PATH}", params=params, headers=_HEADERS,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()
