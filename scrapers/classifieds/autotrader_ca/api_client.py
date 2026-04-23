"""HTTP helpers for AutoTrader.ca — HTML warmup + _next/data list pages.

AutoTrader.ca sits behind Imperva/Incapsula. httpx gets flagged after a few
requests (the server returns an Imperva challenge stub instead of JSON). We
use curl_cffi's Chrome impersonation so the TLS+HTTP fingerprint matches a
real browser and Imperva lets us through.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

from curl_cffi import requests as cffi
from curl_cffi.requests import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    DEFAULT_PAGE_SIZE,
    SITE_ORIGIN,
    build_mode_query,
    slug_for_mode,
)

logger = logging.getLogger(__name__)

# curl_cffi impersonation profile. ``chrome124`` matches the Chrome 147 UA
# closely enough that Imperva accepts the TLS fingerprint.
IMPERSONATE = "chrome124"

_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

_JSON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Nextjs-Data": "1",
}


def new_session() -> AsyncSession:
    """Build a curl_cffi AsyncSession impersonating Chrome.

    Session cookies persist across calls on the same instance — essential
    for Imperva, which sets ``visid_incap_*`` / ``nlbi_*`` cookies on the
    first request and then expects them on subsequent ones.
    """
    return AsyncSession(impersonate=IMPERSONATE, timeout=30)


_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (cffi.exceptions.ConnectionError, cffi.exceptions.Timeout),
    ),
)

# Pull the Next.js ``buildId`` out of the inline ``__NEXT_DATA__`` script.
# The build id rotates on every deploy (minutes), so we discover it on first
# use rather than hard-coding. Anchor is restrictive to avoid matching a
# ``"buildId"`` string somewhere else in the payload.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.S,
)


def _new_search_id() -> str:
    """AutoTrader uses a short alpha-numeric search id to group paginated
    requests. Anything 11 chars works — matches ``24inc4vi3ue`` from the
    browser-captured URL. We mint one per scraper run.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(11))


@retry(**_RETRY)
async def fetch_build_id(session: AsyncSession, mode: str) -> str:
    """Fetch the HTML SRP once and extract ``buildId`` from ``__NEXT_DATA__``.

    The SRP path matches the mode (``/cars/ot_used`` or ``/cars/ot_new``) so
    we warm the same route our subsequent JSON calls will hit. Also primes
    Imperva session cookies for the rest of the run.
    """
    slug = slug_for_mode(mode)
    url = f"{SITE_ORIGIN}/cars/{slug}"
    r = await session.get(url, params=build_mode_query(mode), headers=_HTML_HEADERS)
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("AutoTrader.ca: __NEXT_DATA__ script not found on SRP")
    data = json.loads(m.group(1))
    build_id = data.get("buildId")
    if not build_id:
        raise RuntimeError("AutoTrader.ca: buildId missing from __NEXT_DATA__")
    logger.info("AutoTrader.ca buildId=%s", build_id)
    return build_id


@retry(**_RETRY)
async def fetch_list_page(
    session: AsyncSession,
    *,
    mode: str,
    build_id: str,
    page: int,
    search_id: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Fetch one SRP page via the ``_next/data`` JSON endpoint.

    Returns the ``pageProps`` dict which carries ``{listings, numberOfResults,
    numberOfPages, pageQuery, ...}``. Sort is fixed to ``price DESC`` so
    ``--min-price`` short-circuits pagination cleanly.
    """
    slug = slug_for_mode(mode)
    url = f"{SITE_ORIGIN}/_next/data/{build_id}/legacyTrader/cars/{slug}.json"
    # ``slug=cars`` and ``slug={slug}`` are the Next.js catch-all route
    # params — both must be supplied even though the path already encodes them.
    params = {
        **build_mode_query(mode),
        "search_id": search_id,
        "size": str(page_size),
        "page": str(page),
        "source": "listpage_pagination",
        "slug": ["cars", slug],
    }
    referer = f"{SITE_ORIGIN}/cars/{slug}"
    headers = {**_JSON_HEADERS, "Referer": referer}
    r = await session.get(url, params=params, headers=headers)
    r.raise_for_status()
    # Imperva serves a HTML challenge stub on its reject path even with 200
    # status — json.loads will fail. Surface a clean error instead of the
    # raw "Expecting value" traceback.
    text = r.text
    if text.lstrip().startswith("<"):
        raise RuntimeError(
            "AutoTrader.ca: received HTML (Imperva challenge?) instead of JSON — "
            "session cookies may have expired"
        )
    return (json.loads(text).get("pageProps") or {})
