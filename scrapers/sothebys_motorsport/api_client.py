"""HTTP client — NextAuth login + listings API + listing pageProps (_next/data).

Sotheby's Motorsport sits behind Cloudflare Bot Management which checks the
TLS fingerprint (JA3/JA4) of every connection.  Python's standard TLS stack
has a different fingerprint from real Chrome, so plain httpx returns 403 even
with correct browser headers.

``curl_cffi`` resolves this by using libcurl compiled with BoringSSL (Chrome's
TLS library) and setting ``impersonate="chrome124"`` so the TLS handshake is
byte-for-byte identical to a real Chrome 124 connection.

Session caching
---------------
After a successful login the NextAuth session cookie is saved to
``.sothebys_session_cache.json`` inside ``output/``.  On the next run the
scraper tries to reuse it (validates via ``/api/auth/session``).  If the
session is expired or missing, a fresh login is performed automatically.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import (
    BASE_URL,
    LISTINGS_CALLBACK_URL,
    LISTINGS_ENDPOINT,
    MERGE_LIVE_AND_UPCOMING,
)

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>[^<]+)</script>',
    re.IGNORECASE,
)

_SESSION_CACHE_FILENAME = ".sothebys_session_cache.json"
_TIMEOUT = float(REQUEST_TIMEOUT)  # curl_cffi accepts float seconds


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class SothebysAuthError(RuntimeError):
    """Raised when credentials are provided but login or session validation fails."""


class SothebysApiError(RuntimeError):
    """Raised when an API call returns an unexpected HTTP status or body."""


# ---------------------------------------------------------------------------
# Session cache — persist NextAuth cookies across runs
# ---------------------------------------------------------------------------

def load_session_cache(output_dir: Path) -> dict[str, str]:
    """Load cached cookies from disk.

    Returns a ``{name: value}`` dict (curl_cffi accepts this for ``cookies=``),
    or an empty dict when the cache is missing or has no session token.
    """
    path = output_dir / _SESSION_CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        has_token = any("next-auth.session-token" in k for k in data)
        if not has_token:
            logger.info("Session cache: no session token found — will login fresh.")
            return {}
        logger.info("Session cache: loaded %d cookies.", len(data))
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def save_session_cache(output_dir: Path, session: AsyncSession) -> None:
    """Persist the current curl_cffi session cookies to disk."""
    path = output_dir / _SESSION_CACHE_FILENAME
    try:
        data = {c.name: c.value for c in session.cookies.jar}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("Session cache: saved %d cookies.", len(data))
    except OSError as e:
        logger.warning("Session cache: could not write %s: %s", path, e)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": LISTINGS_CALLBACK_URL,
    }


def _url(path: str) -> str:
    """Prepend base URL to a path."""
    return f"{BASE_URL}{path}"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

async def validate_session(session: AsyncSession) -> bool:
    """Return True if the current cookie jar holds a valid NextAuth session."""
    try:
        r = await session.get(
            _url("/api/auth/session"),
            headers={**default_headers(), "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return False
        body = r.json()
        user = body.get("user") if isinstance(body, dict) else None
        return bool(user and user.get("token"))
    except Exception:
        return False


async def login_with_credentials(session: AsyncSession, email: str, password: str) -> None:
    """Establish a NextAuth session (csrf → credentials → validate).

    Raises SothebysAuthError if the session is not authenticated afterward.
    """
    r = await session.get(
        _url("/api/auth/csrf"),
        headers={**default_headers(), "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise SothebysAuthError(f"CSRF HTTP {r.status_code}: {r.text[:200]!r}")
    csrf = r.json().get("csrfToken")
    if not csrf:
        raise SothebysAuthError("CSRF response missing csrfToken")

    form = {
        "email": email,
        "password": password,
        "remember": "true",
        "redirect": "false",
        "csrfToken": csrf,
        "callbackUrl": LISTINGS_CALLBACK_URL,
        "json": "true",
    }
    r2 = await session.post(
        _url("/api/auth/callback/credentials"),
        data=form,
        headers={**default_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT,
    )
    if r2.status_code != 200:
        raise SothebysAuthError(f"Login callback HTTP {r2.status_code}: {r2.text[:300]!r}")

    r3 = await session.get(
        _url("/api/auth/session"),
        headers={**default_headers(), "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    if r3.status_code != 200:
        raise SothebysAuthError(f"Session HTTP {r3.status_code}: {r3.text[:200]!r}")
    body = r3.json()
    user = body.get("user") if isinstance(body, dict) else None
    if not user or not user.get("token"):
        raise SothebysAuthError("Login succeeded but /api/auth/session returned no user token")


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def _is_retryable_api_error(exc: BaseException) -> bool:
    """Retry on connection errors and 429/5xx HTTP errors."""
    if isinstance(exc, RequestsError):
        return True
    if isinstance(exc, SothebysApiError):
        msg = str(exc)
        return any(code in msg for code in ("429", "500", "502", "503", "504"))
    return False


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((RequestsError, SothebysApiError)),
)
async def fetch_listings_page(
    session: AsyncSession,
    page_num: int,
    limit: int,
    *,
    listing_type: str,
    sort: str,
    merge_live_and_upcoming: bool = MERGE_LIVE_AND_UPCOMING,
) -> dict[str, Any]:
    """One page of ``/api/auctions/listings`` (JSON)."""
    params = {
        "page": str(page_num),
        "limit": str(limit),
        "type": listing_type,
        "mergeLiveAndUpcoming": str(merge_live_and_upcoming).lower(),
        "sort": sort,
    }
    r = await session.get(
        _url(LISTINGS_ENDPOINT),
        params=params,
        headers=default_headers(),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise SothebysApiError(f"listings HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


# ---------------------------------------------------------------------------
# Build ID + detail page props
# ---------------------------------------------------------------------------

async def get_build_id(session: AsyncSession) -> str | None:
    """Discover the Next.js ``buildId`` from the homepage ``__NEXT_DATA__``."""
    try:
        r = await session.get(
            BASE_URL,
            headers={**default_headers(), "Accept": "text/html,application/xhtml+xml"},
            timeout=_TIMEOUT,
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


def _parse_next_data_page_props(html: str) -> dict[str, Any] | None:
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


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(RequestsError),
)
async def fetch_listing_html(session: AsyncSession, slug: str) -> str | None:
    """GET the auction detail page HTML for *slug*."""
    path = f"/auction/{slug}"
    r = await session.get(
        _url(path),
        headers={**default_headers(), "Accept": "text/html,application/xhtml+xml"},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        logger.warning("Auction detail HTML %s returned %s", path, r.status_code)
        return None
    return r.text


async def fetch_detail_page_props(
    session: AsyncSession,
    slug: str,
    build_id: str | None = None,
) -> dict[str, Any] | None:
    """Return ``pageProps`` for ``/auction/{slug}``.

    Prefers ``/_next/data/{build_id}/auction/{slug}.json`` (no HTML parsing).
    Falls back to full HTML + ``__NEXT_DATA__`` extraction.
    """
    if build_id:
        path = f"/_next/data/{build_id}/auction/{slug}.json"
        try:
            r = await session.get(
                _url(path),
                params={"auction": slug},
                headers=default_headers(),
                timeout=_TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                pp = data.get("pageProps")
                return pp if isinstance(pp, dict) else None
            logger.warning("_next/data for %s returned %s — falling back to HTML", slug, r.status_code)
        except Exception as e:
            logger.warning("_next/data fetch failed for %s: %s — falling back to HTML", slug, e)

    html = await fetch_listing_html(session, slug)
    if not html:
        return None
    return _parse_next_data_page_props(html)
