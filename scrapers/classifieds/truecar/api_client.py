"""HTTP helpers for TrueCar — curl_cffi session + GraphQL search + VDP HTML.

TrueCar sits behind PerimeterX. httpx/raw-python clients get 403'd immediately
because PX fingerprints TLS (JA3) + HTTP/2 frames. We use curl_cffi's Chrome
impersonation which matches real Chrome on the wire; PX treats it as a real
browser. A warmup sequence (homepage → SRP → tiny wait) is still needed so
PX sees a "natural" user flow before our GraphQL POST.

When the datacenter IP is blocked at the IP level (homepage itself 403s),
a residential proxy from the ``proxies`` Supabase table is required.
Pass ``proxy_url`` to ``new_session()`` to route through one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
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

# curl_cffi impersonation profile. Use a recent Chrome build; PX fingerprint
# databases update over time so we track the newest available profile.
IMPERSONATE = "chrome136"

# SRP URLs per mode — the Referer we send AND the page we warm up on has to
# match the condition we're about to query, otherwise PerimeterX 403s the
# POST as a suspicious context-switch.
_SRP_URL_BY_MODE = {
    "used": f"{SITE_ORIGIN}/used-cars-for-sale/listings/?sort[]=price_desc",
    "new": f"{SITE_ORIGIN}/new-cars-for-sale/listings/?sort[]=price_desc",
}


def _graphql_headers(mode: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json",
        "Origin": SITE_ORIGIN,
        "Referer": _SRP_URL_BY_MODE.get(mode, _SRP_URL_BY_MODE["used"]),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        # Non-optional — without these the endpoint 403s.
        "apollographql-client-name": CLIENT_NAME,
        "authorization-mode": AUTHORIZATION_MODE,
        "x-gql-operation-name": OPERATION_NAME,
    }


# Retry only on transport + 5xx. A 403 from PX is deterministic — retrying
# without a fresh warmup won't help; we'd rather fail fast and let the caller
# decide.
_RETRY_EXCEPTIONS = (cffi.exceptions.ConnectionError, cffi.exceptions.Timeout)


def new_session(proxy_url: str | None = None) -> AsyncSession:
    """Build a curl_cffi AsyncSession impersonating Chrome.

    Pass ``proxy_url`` (``http://user:pass@host:port``) to route through a
    residential proxy — required when the datacenter IP is PX-blocked.
    Call ``warmup(session)`` on it before issuing GraphQL POSTs.
    """
    kwargs: dict[str, Any] = {"impersonate": IMPERSONATE, "timeout": 30}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return AsyncSession(**kwargs)


_WARMUP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


async def warmup(session: AsyncSession, mode: str = "used") -> None:
    """Prime PerimeterX: visit homepage, then the mode-matching SRP, to
    populate session cookies and pass PX's "is this a real user flow?" heuristics.

    The SRP warmup URL must match ``mode`` — warming up on ``/used-cars-...``
    and then POSTing a ``condition: NEW`` query triggers a 403.
    """
    r = await session.get(f"{SITE_ORIGIN}/", headers=_WARMUP_HEADERS)
    if r.status_code == 403:
        raise RuntimeError(
            f"TrueCar homepage returned 403 — datacenter IP blocked by PerimeterX. "
            "Add residential proxies to the 'proxies' Supabase table."
        )
    await asyncio.sleep(1.2 + random.uniform(0.3, 0.8))
    srp_headers = {**_WARMUP_HEADERS, "Sec-Fetch-Site": "same-origin", "Referer": f"{SITE_ORIGIN}/"}
    await session.get(_SRP_URL_BY_MODE.get(mode, _SRP_URL_BY_MODE["used"]), headers=srp_headers)
    # Give PX cookies time to settle and simulate reading time before XHR.
    await asyncio.sleep(2.0 + random.uniform(0.5, 1.5))


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
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the VDP HTML and return ``(next_data_dict, raw_html)``.

    ``next_data_dict`` is the parsed ``__NEXT_DATA__`` JSON (or None).
    ``raw_html`` is the full page text, used as fallback for seller notes.
    Returns ``(None, None)`` if the listing is gone (404).
    """
    url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, vin=vin)
    r = await session.get(url, headers=_HTML_HEADERS)
    if r.status_code != 200:
        return None, None
    html_text = r.text
    m = _NEXT_DATA_RE.search(html_text)
    if not m:
        return None, html_text
    try:
        return json.loads(m.group(1)), html_text
    except json.JSONDecodeError as e:
        logger.debug("NEXT_DATA parse failed for %s: %s", vin, e)
        return None, html_text
