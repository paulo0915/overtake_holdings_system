"""HTTP client — NextAuth login (optional) + listings API + listing HTML (__NEXT_DATA__)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from shared.config import REQUEST_TIMEOUT

from .config import BASE_URL, LISTINGS_CALLBACK_URL, LISTINGS_ENDPOINT

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>[^<]+)</script>',
    re.IGNORECASE,
)


class DuPontAuthError(RuntimeError):
    """Raised when credentials are provided but login or session validation fails."""


def default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Origin": BASE_URL,
    }


async def login_with_credentials(client: httpx.AsyncClient, email: str, password: str) -> None:
    """
    Establish NextAuth session cookies on ``client`` (csrf → credentials → session).

    Raises DuPontAuthError if the session is not authenticated afterward.
    """
    headers = {
        **default_headers(),
        "Referer": LISTINGS_CALLBACK_URL,
        "Content-Type": "application/json",
    }
    r = await client.get("/api/auth/csrf", headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    csrf = r.json().get("csrfToken")
    if not csrf:
        raise DuPontAuthError("CSRF response missing csrfToken")

    form = {
        "email": email,
        "password": password,
        "remember": "true",
        "redirect": "false",
        "csrfToken": csrf,
        "callbackUrl": LISTINGS_CALLBACK_URL,
        "json": "true",
    }
    r2 = await client.post(
        "/api/auth/callback/credentials",
        data=form,
        headers={
            **default_headers(),
            "Referer": LISTINGS_CALLBACK_URL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r2.raise_for_status()

    r3 = await client.get(
        "/api/auth/session",
        headers={**default_headers(), "Referer": LISTINGS_CALLBACK_URL, "Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    r3.raise_for_status()
    body = r3.json()
    user = body.get("user") if isinstance(body, dict) else None
    # Session response has: {"user": {"emailVerified": bool, "token": "...", ...}}
    # There is no "email" key — check "token" which is always present for a valid session.
    if not user or not user.get("token"):
        raise DuPontAuthError("Login succeeded but /api/auth/session returned no user token")


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_listings_page(
    client: httpx.AsyncClient,
    page: int,
    limit: int,
    *,
    listing_type: str,
    sort: str,
) -> dict[str, Any]:
    """One page of ``/api/auctions/listings`` (JSON)."""
    params = {"page": page, "limit": limit, "type": listing_type, "sort": sort}
    r = await client.get(
        LISTINGS_ENDPOINT,
        params=params,
        headers={**default_headers(), "Referer": LISTINGS_CALLBACK_URL},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


async def get_build_id(client: httpx.AsyncClient) -> str | None:
    """Fetch the homepage to discover the current Next.js build ID."""
    try:
        r = await client.get(
            "/",
            headers={**default_headers(), "Accept": "text/html,application/xhtml+xml"},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        m = _NEXT_DATA_RE.search(r.text)
        if m:
            payload = json.loads(m.group("json"))
            build_id = payload.get("buildId")
            if build_id:
                return str(build_id)
    except Exception as e:
        logger.warning("Could not fetch build ID: %s", e)
    return None


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
)
async def fetch_listing_html(client: httpx.AsyncClient, slug: str) -> str | None:
    """GET the auction detail page HTML for ``slug``.

    Uses /auction/{slug} which — unlike /listings/sold/{slug} — returns a
    server-rendered page with ``pageProps.auctionData`` populated (VIN, engine,
    colours, mileage, etc.).  Returns None for non-200 responses.
    """
    path = f"/auction/{slug}"
    r = await client.get(
        path,
        headers={**default_headers(), "Referer": LISTINGS_CALLBACK_URL},
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    if r.status_code != 200:
        logger.warning("Auction detail HTML %s returned %s", path, r.status_code)
        return None
    return r.text


def parse_next_data_page_props(html: str) -> dict[str, Any] | None:
    """Extract ``props.pageProps`` from embedded ``__NEXT_DATA__``."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        payload = json.loads(m.group("json"))
    except json.JSONDecodeError:
        return None
    props = payload.get("props") or {}
    pp = props.get("pageProps")
    return pp if isinstance(pp, dict) else None


async def fetch_detail_page_props(
    client: httpx.AsyncClient,
    slug: str,
    build_id: str | None = None,
) -> dict[str, Any] | None:
    """Return ``pageProps`` for ``/auction/{slug}``.

    Prefers the lightweight ``/_next/data/{build_id}/auction/{slug}.json`` JSON
    endpoint when *build_id* is available (no HTML parsing overhead).  Falls
    back to fetching the full HTML and parsing ``__NEXT_DATA__``.
    """
    if build_id:
        path = f"/_next/data/{build_id}/auction/{slug}.json"
        try:
            r = await client.get(
                path,
                headers={**default_headers(), "Referer": LISTINGS_CALLBACK_URL},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                pp = data.get("pageProps")
                return pp if isinstance(pp, dict) else None
            logger.warning(
                "_next/data for %s returned %s — falling back to HTML", slug, r.status_code
            )
        except Exception as e:
            logger.warning("_next/data fetch failed for %s: %s — falling back to HTML", slug, e)

    html = await fetch_listing_html(client, slug)
    if not html:
        return None
    return parse_next_data_page_props(html)
