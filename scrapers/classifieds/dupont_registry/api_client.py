"""HTTP helpers for duPont REGISTRY — GraphQL listing search + detail HTML."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    DEFAULT_PAGE_SIZE,
    DETAIL_URL_TEMPLATE,
    GRAPHQL_FIELDS,
    GRAPHQL_PATH,
    SITE_ORIGIN,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

_GRAPHQL_QUERY = (
    "query ExecuteQuery2($queryName: String!, $fields: String!, $variables: String, $authToken: String) {\n"
    "  executeQuery2(\n"
    "    queryName: $queryName\n"
    "    fields: $fields\n"
    "    variables: $variables\n"
    "    authToken: $authToken\n"
    "  )\n"
    "}"
)

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


def _build_variables_string(*, offset: int, limit: int) -> str:
    """``variables`` on this endpoint is a stringified JSON payload."""
    payload = {
        "includes": "brand,model,locations,dealer,condition",
        "source": "es",
        "limit": limit,
        "offset": offset,
        "filteredAggregations": [],
        "aggregations": [],
        "filters": {},
        "cache": False,
        "sort": [{"field": "price", "order": "DESC"}],
    }
    return json.dumps(payload, separators=(",", ":"))


@retry(**_RETRY)
async def post_search_cars(
    client: httpx.AsyncClient,
    *,
    offset: int,
    limit: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Call ``getCars`` via the ``executeQuery2`` wrapper. Returns the parsed
    inner payload (with ``data`` + ``pagination`` keys) — the outer envelope
    is unwrapped here so callers don't have to do the double-JSON dance.
    """
    url = f"{SITE_ORIGIN}{GRAPHQL_PATH}"
    body = {
        "operationName": "ExecuteQuery2",
        "variables": {
            "queryName": "getCars",
            "fields": GRAPHQL_FIELDS,
            "variables": _build_variables_string(offset=offset, limit=limit),
        },
        "extensions": {
            "clientLibrary": {"name": "@apollo/client", "version": "4.0.2"},
        },
        "query": _GRAPHQL_QUERY,
    }
    headers = {
        "User-Agent": _UA,
        "Accept": "application/graphql-response+json,application/json;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": SITE_ORIGIN,
        "Referer": f"{SITE_ORIGIN}/cars-for-sale/all?sort=price_desc",
    }
    r = await client.post(url, json=body, headers=headers)
    r.raise_for_status()
    outer = r.json()
    # Outer shape: ``{"data": {"executeQuery2": "<stringified inner JSON>"}}``.
    inner_str = (outer.get("data") or {}).get("executeQuery2") or ""
    try:
        inner = json.loads(inner_str) if inner_str else {}
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse executeQuery2 inner JSON: %s", e)
        return {}
    # The payload wraps everything under ``cars`` because ``queryName=getCars``.
    return inner.get("cars") or {}


@retry(**_RETRY)
async def fetch_detail_html(
    client: httpx.AsyncClient,
    *,
    brand_alias: str,
    model_alias: str,
    year: int,
    vin: str,
    car_id: str,
) -> str:
    """GET the listing detail page; we parse description + JSON-LD from the HTML."""
    url = DETAIL_URL_TEMPLATE.format(
        origin=SITE_ORIGIN,
        brand_alias=brand_alias,
        model_alias=model_alias,
        year=year,
        vin=vin,
        car_id=car_id,
    )
    r = await client.get(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{SITE_ORIGIN}/cars-for-sale/all?sort=price_desc",
        },
    )
    r.raise_for_status()
    return r.text
