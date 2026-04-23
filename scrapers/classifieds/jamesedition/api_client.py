"""HTTP helpers for JamesEdition (curl_cffi Chrome impersonation)."""

from __future__ import annotations

import logging

from curl_cffi import requests as cffi
from curl_cffi.requests import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    DEFAULT_PAGE_SIZE,
    LIST_PATH,
    SITE_ORIGIN,
    SORT_PARAM_NAME,
    SORT_PRICE_DESC,
)

logger = logging.getLogger(__name__)

# Chrome impersonation — JamesEdition sits behind Cloudflare. A UA-only
# httpx request gets a js-challenge page; curl_cffi handshakes past it.
IMPERSONATE = "chrome124"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_ORIGIN}/cars",
}

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (cffi.exceptions.ConnectionError, cffi.exceptions.Timeout),
    ),
)


def new_session() -> AsyncSession:
    """curl_cffi session impersonating Chrome 124. Reuse across calls so
    Cloudflare clearance cookies persist.
    """
    return AsyncSession(impersonate=IMPERSONATE, timeout=45)


@retry(**_RETRY)
async def fetch_list_page(session: AsyncSession, *, page: int) -> str:
    """Fetch one SRP HTML page sorted ``price_desc``.

    Returns the raw HTML body. ``page`` starts at 1.
    """
    params = {SORT_PARAM_NAME: SORT_PRICE_DESC, "page": str(page)}
    r = await session.get(
        f"{SITE_ORIGIN}{LIST_PATH}", params=params, headers=_HEADERS,
    )
    r.raise_for_status()
    return r.text


@retry(**_RETRY)
async def fetch_detail_html(session: AsyncSession, *, url: str) -> str | None:
    """Fetch a detail HTML page. Returns ``None`` if the listing was pulled
    (404) between the list + detail calls.
    """
    r = await session.get(url, headers=_HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text
