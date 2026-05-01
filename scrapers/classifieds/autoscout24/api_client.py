"""HTTP helpers for AutoScout24 Next.js ``_next/data`` JSON."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT, get_headers

from .config import LIST_QUERY_BASE, SITE_ORIGIN

logger = logging.getLogger(__name__)

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)

_BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([^"]+)"')


def _headers_json(*, referer: str = "") -> dict[str, str]:
    h = get_headers(accept="*/*", referer=referer or SITE_ORIGIN + "/")
    h["x-nextjs-data"] = "1"
    h.setdefault("Accept-Language", "en-GB,en;q=0.9")
    return h


@retry(**_RETRY)
async def fetch_build_id(client: httpx.AsyncClient, *, list_path: str) -> str:
    """GET ``/lst?...`` HTML and extract Next.js ``buildId``."""
    url = f"{SITE_ORIGIN}{list_path}"
    resp = await client.get(url, headers=_headers_json(referer=SITE_ORIGIN + "/"), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    m = _BUILD_ID_RE.search(resp.text)
    if not m:
        raise RuntimeError("buildId not found in AutoScout24 list HTML")
    return m.group(1)


def list_json_url(build_id: str, *, page: int, ustate: str, search_id: str | None) -> str:
    """``ustate``: ``U``, ``N``, or ``N%2CU`` (URL-encoded comma)."""
    q = f"{LIST_QUERY_BASE}&ustate={ustate}&page={page}"
    if search_id and page > 1:
        q += f"&search_id={search_id}&source=listpage_pagination"
    return f"{SITE_ORIGIN}/_next/data/{build_id}/lst.json?{q}"


@retry(**_RETRY)
async def fetch_list_json(
    client: httpx.AsyncClient,
    *,
    build_id: str,
    page: int,
    ustate: str,
    search_id: str | None,
    referer: str,
) -> dict[str, Any]:
    url = list_json_url(build_id, page=page, ustate=ustate, search_id=search_id)
    resp = await client.get(url, headers=_headers_json(referer=referer), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def detail_json_url(build_id: str, slug_after_offers: str) -> str:
    """``slug_after_offers`` = everything after ``/offers/`` in the listing path."""
    return f"{SITE_ORIGIN}/_next/data/{build_id}/details/{slug_after_offers}.json"


@retry(**_RETRY)
async def fetch_detail_json(
    client: httpx.AsyncClient,
    *,
    build_id: str,
    offer_path: str,
) -> dict[str, Any] | None:
    """GET detail ``pageProps.listingDetails`` JSON (``offer_path`` starts with ``/offers/``)."""
    if "/offers/" not in offer_path:
        return None
    slug = offer_path.split("/offers/", 1)[-1]
    url = detail_json_url(build_id, slug)
    ref = f"{SITE_ORIGIN}{offer_path}"
    try:
        resp = await client.get(url, headers=_headers_json(referer=ref), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return (data.get("pageProps") or {}).get("listingDetails") or None
    except Exception as e:
        logger.warning("Detail fetch failed %s: %s", url, e)
        return None
