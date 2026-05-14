"""HTTP client — Car & Classic Inertia JSON (httpx + session cookies)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import (
    AUCTION_RESULTS_URL,
    INERTIA_DETAIL_COMPONENT,
    INERTIA_SEARCH_COMPONENT,
    LISTING_PARTIAL_DATA,
    RESULTS_REFERER,
    SEARCH_BASE_PARAMS,
    SEARCH_PARTIAL_DATA,
    SITE_ORIGIN,
)

logger = logging.getLogger(__name__)

_TIMEOUT = float(REQUEST_TIMEOUT)
_VERSION_RE = re.compile(r'"version":"([a-f0-9]{32})"')

_inertia_version: str | None = None


class CarAndClassicApiError(RuntimeError):
    """Raised when an Inertia/API call fails."""


def _xsrf_header(client: httpx.AsyncClient) -> dict[str, str]:
    raw = client.cookies.get("XSRF-TOKEN")
    if not raw:
        return {}
    return {"x-xsrf-token": unquote(str(raw))}


def _inertia_headers(
    client: httpx.AsyncClient,
    *,
    component: str,
    partial_data: str,
    referer: str,
) -> dict[str, str]:
    if not _inertia_version:
        raise CarAndClassicApiError("Inertia version not set — call bootstrap_inertia first")
    h: dict[str, str] = {
        "accept": "text/html, application/xhtml+xml",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "x-inertia": "true",
        "x-inertia-partial-component": component,
        "x-inertia-partial-data": partial_data,
        "x-requested-with": "XMLHttpRequest",
        "referer": referer,
        "x-inertia-version": _inertia_version,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }
    h.update(_xsrf_header(client))
    return h


_DOCUMENT_HEADERS = {
    "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}


async def bootstrap_inertia(client: httpx.AsyncClient) -> str:
    """Warm session on home + results HTML; read Inertia ``version`` hash."""
    global _inertia_version

    await client.get(
        f"{SITE_ORIGIN}/",
        headers={**_DOCUMENT_HEADERS, "referer": SITE_ORIGIN + "/"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    r = await client.get(
        AUCTION_RESULTS_URL,
        headers={**_DOCUMENT_HEADERS, "referer": f"{SITE_ORIGIN}/"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code != 200:
        raise CarAndClassicApiError(f"bootstrap HTTP {r.status_code}")
    m = _VERSION_RE.search(r.text)
    if not m:
        raise CarAndClassicApiError("Could not find Inertia version in results HTML")
    _inertia_version = m.group(1)
    logger.info("Car & Classic Inertia version %s…", _inertia_version[:12])
    return _inertia_version


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_results_page(
    client: httpx.AsyncClient,
    *,
    page: int,
    min_price_minor: int = 0,
) -> dict[str, Any]:
    """Fetch one Inertia results page.

    ``min_price_minor`` is sent as the ``min_price`` query param in the
    site's MINOR currency unit (GBP-pence). Caller is responsible for
    converting from USD if needed — this layer is currency-agnostic.
    """
    referer = f"{RESULTS_REFERER}?page={page}" if page > 1 else RESULTS_REFERER
    params: dict[str, str | int] = {**SEARCH_BASE_PARAMS, "page": page}
    if min_price_minor > 0:
        params["min_price"] = int(min_price_minor)
    r = await client.get(
        AUCTION_RESULTS_URL,
        params=params,
        headers=_inertia_headers(
            client,
            component=INERTIA_SEARCH_COMPONENT,
            partial_data=SEARCH_PARTIAL_DATA,
            referer=referer,
        ),
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code == 409:
        raise CarAndClassicApiError("Inertia 409 — version mismatch; try re-bootstrapping")
    if r.status_code != 200:
        raise CarAndClassicApiError(f"results page {page} HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception as e:
        raise CarAndClassicApiError(f"results page {page} JSON error: {e}") from e


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=12),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_listing_partial(client: httpx.AsyncClient, *, auction_path: str) -> dict[str, Any] | None:
    """Document GET then Inertia partial; on failure use meta-description HTML fallback."""
    from .listing_parser import enrich_listing_partial_with_document_vrm, listing_payload_from_document_html

    if not auction_path.startswith("/"):
        auction_path = "/" + auction_path
    url = f"{SITE_ORIGIN}{auction_path}"

    r0 = await client.get(
        url,
        headers={**_DOCUMENT_HEADERS, "referer": RESULTS_REFERER},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    html = r0.text if r0.status_code == 200 else ""

    r = await client.get(
        url,
        headers=_inertia_headers(
            client,
            component=INERTIA_DETAIL_COMPONENT,
            partial_data=LISTING_PARTIAL_DATA,
            referer=url,
        ),
        timeout=_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code == 200:
        try:
            j = r.json()
            listing = (j.get("props") or {}).get("listing")
            if isinstance(j, dict) and isinstance(listing, dict) and listing:
                return enrich_listing_partial_with_document_vrm(j, html)
        except Exception:
            pass

    fb = listing_payload_from_document_html(html)
    if fb:
        logger.debug("Using HTML meta fallback for %s", auction_path)
    return fb
