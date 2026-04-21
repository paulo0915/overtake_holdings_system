"""HTTP client — The Market JSON API (list + listing detail)."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import (
    ACCESS_TOKEN_FALLBACK,
    DETAIL_INCLUDE,
    LISTING_DETAIL_URL,
    LISTINGS_RESULTS_URL,
    RESULTS_REFERER_BASE,
    SITE_ORIGIN,
    THE_MARKET_ACCESS_TOKEN,
)

logger = logging.getLogger(__name__)

_TIMEOUT = float(REQUEST_TIMEOUT)

# ``window.__NUXT__.config={public:{accessToken:"…",…}}`` in the HTML shell (same value the SPA sends).
_TOKEN_PATTERNS = (
    re.compile(r'public:\{accessToken:"([^"]+)"'),
    re.compile(r'accessToken\s*:\s*"([^"]+)"'),
)

_access_token: str | None = None


class TheMarketApiError(RuntimeError):
    """Raised when an API call fails or returns unexpected JSON."""


def _parse_token_from_html(html: str) -> str | None:
    for pat in _TOKEN_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1).strip()
    return None


def _current_access_token() -> str:
    if _access_token is not None:
        return _access_token
    if THE_MARKET_ACCESS_TOKEN and THE_MARKET_ACCESS_TOKEN.strip():
        return THE_MARKET_ACCESS_TOKEN.strip()
    return ACCESS_TOKEN_FALLBACK


async def resolve_access_token(client: httpx.AsyncClient) -> str:
    """
    Set the ``accesstoken`` header for this run.

    Order: ``THE_MARKET_ACCESS_TOKEN`` env (optional) → parse from ``/auctions/results`` HTML
    → bundled fallback.
    """
    global _access_token

    if THE_MARKET_ACCESS_TOKEN and THE_MARKET_ACCESS_TOKEN.strip():
        _access_token = THE_MARKET_ACCESS_TOKEN.strip()
        logger.info("Using THE_MARKET_ACCESS_TOKEN from environment")
        return _access_token

    r = await client.get(
        RESULTS_REFERER_BASE,
        headers={
            "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
            ),
        },
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code != 200:
        logger.warning(
            "Could not load results page for token (HTTP %s); using fallback",
            r.status_code,
        )
        _access_token = ACCESS_TOKEN_FALLBACK
        return _access_token

    parsed = _parse_token_from_html(r.text)
    if not parsed:
        logger.warning("accessToken not found in HTML; using bundled fallback")
        _access_token = ACCESS_TOKEN_FALLBACK
        return _access_token

    _access_token = parsed
    logger.info("Resolved API access token from site HTML")
    return _access_token


def _headers(*, referer: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "referer": referer,
        "accesstoken": _current_access_token(),
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_results_page(client: httpx.AsyncClient, *, page: int) -> dict[str, Any]:
    referer = f"{RESULTS_REFERER_BASE}?page={page}"
    r = await client.get(
        LISTINGS_RESULTS_URL,
        params={"page": page},
        headers=_headers(referer=referer),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise TheMarketApiError(f"results page {page} HTTP {r.status_code}: {r.text[:400]}")
    try:
        return r.json()
    except Exception as e:
        raise TheMarketApiError(f"results page {page} JSON error: {e}") from e


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_listing_detail(
    client: httpx.AsyncClient,
    listing_id: str,
    *,
    referer_path: str,
) -> dict[str, Any] | None:
    """GET one listing; ``referer_path`` should look like ``/listings/make/model/id``."""
    url = f"{LISTING_DETAIL_URL}/{listing_id}"
    referer = f"{SITE_ORIGIN}{referer_path}" if referer_path.startswith("/") else referer_path
    r = await client.get(
        url,
        params={"include": DETAIL_INCLUDE},
        headers=_headers(referer=referer),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        logger.warning("listing %s HTTP %s", listing_id, r.status_code)
        return None
    try:
        payload = r.json()
    except Exception as e:
        logger.warning("listing %s JSON error: %s", listing_id, e)
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data
