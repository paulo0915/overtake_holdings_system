"""HTTP helpers for TrueCar — curl_cffi session + GraphQL search + VDP HTML.

TrueCar sits behind PerimeterX. httpx/raw-python clients get 403'd immediately
because PX fingerprints TLS (JA3) + HTTP/2 frames. We use curl_cffi's Chrome
impersonation which matches real Chrome on the wire; PX treats it as a real
browser. A warmup sequence (homepage → SRP → tiny wait) is still needed so
PX sees a "natural" user flow before our GraphQL POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from curl_cffi import requests as cffi
from curl_cffi.requests import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    AUTHORIZATION_MODE,
    CLIENT_NAME,
    DEFAULT_PAGE_SIZE,
    DEFAULT_POSTAL_CODE,
    DEFAULT_RADIUS_MILES,
    DETAIL_URL_TEMPLATE,
    GRAPHQL_URL,
    OPERATION_NAME,
    SITE_ORIGIN,
    condition_for_mode,
)
from .queries import LIST_QUERY

logger = logging.getLogger(__name__)

# curl_cffi impersonation profile. "chrome124" is close enough to the UA we
# send and matches the modern TLS fingerprint PX expects.
IMPERSONATE = "chrome124"

# SRP URLs per mode — the Referer we send AND the page we warm up on has to
# match the condition we're about to query, otherwise PerimeterX 403s the
# POST as a suspicious context-switch.
_SRP_URL_BY_MODE = {
    "used": f"{SITE_ORIGIN}/used-cars-for-sale/listings/?city=santa-monica&state=ca&sort[]=price_desc",
    "new": f"{SITE_ORIGIN}/new-cars-for-sale/listings/?city=santa-monica&state=ca&sort[]=price_desc",
}


def _graphql_headers(mode: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": SITE_ORIGIN,
        "Referer": _SRP_URL_BY_MODE.get(mode, _SRP_URL_BY_MODE["used"]),
        # Non-optional — without these the endpoint 403s.
        "apollographql-client-name": CLIENT_NAME,
        "authorization-mode": AUTHORIZATION_MODE,
        "x-gql-operation-name": OPERATION_NAME,
    }

# Retry only on transport + 5xx. A 403 from PX is deterministic — retrying
# without a fresh warmup won't help; we'd rather fail fast and let the caller
# decide.
_RETRY_EXCEPTIONS = (cffi.exceptions.ConnectionError, cffi.exceptions.Timeout)


def new_session() -> AsyncSession:
    """Build a curl_cffi AsyncSession impersonating Chrome.

    Call ``warmup(session)`` on it before issuing GraphQL POSTs.
    """
    return AsyncSession(impersonate=IMPERSONATE, timeout=30)


async def warmup(session: AsyncSession, mode: str = "used") -> None:
    """Prime PerimeterX: visit homepage, then the mode-matching SRP, to
    populate session cookies and pass PX's "is this a real user flow?" heuristics.

    The SRP warmup URL must match ``mode`` — warming up on ``/used-cars-...``
    and then POSTing a ``condition: NEW`` query triggers a 403.
    """
    await session.get(f"{SITE_ORIGIN}/")
    await asyncio.sleep(0.6)
    await session.get(_SRP_URL_BY_MODE.get(mode, _SRP_URL_BY_MODE["used"]))
    # Small pause — helps PX cookies settle before the XHR fires.
    await asyncio.sleep(1.0)


def _check_gql_errors(payload: dict[str, Any], op: str) -> None:
    errs = payload.get("errors")
    if errs:
        msg = errs[0].get("message") if isinstance(errs[0], dict) else str(errs[0])
        raise RuntimeError(f"TrueCar {op} GraphQL error: {msg}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(_RETRY_EXCEPTIONS),
)
async def post_search_page(
    session: AsyncSession,
    *,
    mode: str,
    first: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch one page of ``marketplaceListingSearch``. Returns
    ``{edges, pageInfo, totalCount}`` from the GraphQL response. Sort is fixed
    to ``PRICE_DESC`` so ``--min-price`` can short-circuit pagination.
    """
    condition = condition_for_mode(mode)
    body = {
        "operationName": OPERATION_NAME,
        "variables": {
            "filters": {
                "condition": condition,
                "truecarPlus": False,
                "fallbackStrategy": "SIMPLE",
                "withinRadius": {
                    "postalCode": DEFAULT_POSTAL_CODE,
                    "distance": DEFAULT_RADIUS_MILES,
                },
            },
            "first": first,
            "offset": offset,
            "sort": "PRICE_DESC",
            "galleryImagesCount": 5,
        },
        "query": LIST_QUERY,
    }
    r = await session.post(GRAPHQL_URL, json=body, headers=_graphql_headers(mode))
    if r.status_code == 403:
        raise RuntimeError(
            "TrueCar returned 403 (PerimeterX block) — warmup may have "
            "expired or impersonation is stale"
        )
    r.raise_for_status()
    payload = r.json()
    _check_gql_errors(payload, OPERATION_NAME)
    return (payload.get("data") or {}).get("marketplaceListingSearch") or {}


# --- Detail (VDP) fetch ------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.S,
)

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_ORIGIN}/used-cars-for-sale/listings/",
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(_RETRY_EXCEPTIONS),
)
async def fetch_detail_html(
    session: AsyncSession,
    *,
    vin: str,
) -> dict[str, Any] | None:
    """Fetch the VDP HTML and return the parsed ``__NEXT_DATA__`` JSON.

    Returns ``None`` if the listing is gone (404) or the page didn't render
    the expected script tag.
    """
    url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, vin=vin)
    r = await session.get(url, headers=_HTML_HEADERS)
    if r.status_code != 200:
        return None
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.debug("NEXT_DATA parse failed for %s: %s", vin, e)
        return None
