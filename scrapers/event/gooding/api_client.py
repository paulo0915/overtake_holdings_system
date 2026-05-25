"""HTTP helpers for Gooding & Company — homepage, app bundle, lot page-data JSON."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import (
    APP_JS_HREF_RE,
    GRAPHQL_OPERATION,
    GRAPHQL_PERSISTED_HASH,
    GRAPHQL_URL,
    PAGE_DATA_PATH_TEMPLATE,
    SITE_ORIGIN,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_COMMON_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

_APP_JS_RE = re.compile(APP_JS_HREF_RE)

# Transient-failure retry policy.
_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException),
    ),
)


@retry(**_RETRY)
async def fetch_home_html(client: httpx.AsyncClient) -> str:
    r = await client.get(SITE_ORIGIN + "/", headers=_COMMON_HEADERS)
    r.raise_for_status()
    return r.text


def find_app_js_path(home_html: str) -> str | None:
    """Locate the current ``/app-*.js`` bundle path (hash changes per deploy)."""
    m = _APP_JS_RE.search(home_html)
    return m.group(0) if m else None


@retry(**_RETRY)
async def fetch_app_js(client: httpx.AsyncClient, app_js_path: str) -> str:
    url = SITE_ORIGIN + app_js_path
    r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


@retry(**_RETRY)
async def fetch_page_data(
    client: httpx.AsyncClient, *, path: str,
) -> dict[str, Any]:
    """Fetch ``/page-data/<path>/page-data.json`` for a lot or auction page."""
    url = SITE_ORIGIN + PAGE_DATA_PATH_TEMPLATE.format(path=path)
    r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.json()


@retry(**_RETRY)
async def fetch_lots_page_data(client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Fetch ``/page-data/lots/page-data.json`` — Gatsby catalog index.

    Returns the parsed JSON dict or None when the endpoint returns a non-JSON
    response (e.g. Gatsby 404 HTML page).
    """
    url = SITE_ORIGIN + "/page-data/lots/page-data.json"
    try:
        r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("fetch_lots_page_data failed: %s", e)
        return None


# --- Upcoming-auction discovery ----------------------------------------------

# ``<li><a href="/auction/pebble-beach-auctions-2026">Pebble Beach</a></li>``
# in the navbar AUCTIONS dropdown. Excludes the two non-auction entries
# (``/geared-online-auctions`` and ``/results``) by matching only the
# ``/auction/<slug>`` shape.
_NAVBAR_AUCTION_HREF_RE = re.compile(
    r'href=["\'](/auction/[a-z0-9][a-z0-9\-]+)/?["\']',
    re.I,
)

# "View Early Consignments" button on an auction page — its presence is the
# canonical signal that an upcoming auction has published its preview
# catalogue (past auctions and far-future auctions omit it).
_EARLY_CONSIGNMENTS_RE = re.compile(
    r"View\s+Early\s+Consignments",
    re.I,
)


def parse_navbar_auction_paths(home_html: str) -> list[str]:
    """Return unique ``/auction/<slug>`` paths from the homepage navbar.

    Order-preserving dedupe so the auction list matches navbar order. Skips
    Geared Online and Auction Results entries which use different href shapes.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _NAVBAR_AUCTION_HREF_RE.finditer(home_html):
        p = m.group(1).rstrip("/")
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def has_early_consignments_button(auction_html: str) -> bool:
    """True when the auction page renders a 'View Early Consignments' anchor."""
    return bool(_EARLY_CONSIGNMENTS_RE.search(auction_html or ""))


@retry(**_RETRY)
async def fetch_auction_html(client: httpx.AsyncClient, *, path: str) -> str:
    """GET ``/auction/<slug>/`` — rendered HTML (used only to check for the
    'View Early Consignments' button; lot data comes from page-data JSON)."""
    p = path if path.startswith("/") else f"/{path}"
    if not p.endswith("/"):
        p += "/"
    url = SITE_ORIGIN + p
    r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


@retry(**_RETRY)
async def fetch_auction_page_data(
    client: httpx.AsyncClient, *, path: str,
) -> dict[str, Any] | None:
    """Fetch the per-auction Gatsby payload so we can pull ``WebPageAuction.title``
    + ``auction.contentful_id`` — the contentful_id is the join key we use
    to match lots in the bundle against this specific auction.
    """
    p = path if path.startswith("/") else f"/{path}"
    if not p.endswith("/"):
        p += "/"
    url = SITE_ORIGIN + PAGE_DATA_PATH_TEMPLATE.format(path=p.rstrip("/"))
    try:
        r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("fetch_auction_page_data(%s) failed: %s", path, e)
        return None


# --- Early-consignment GraphQL discovery -------------------------------------

# The 'View Early Consignments' anchor href looks like:
#   /lots/?filtersInput[auctionType][0]=Live%20Auction
#         &filtersInput[auctionYear][0]=2026
#         &filtersInput[liveAuctionVenue][0]=Pebble%20Beach
#         &sortBy=ENDING_SOONEST
# We parse that query string into the GraphQL filtersInput object.
_EC_BUTTON_RE = re.compile(
    r'href=["\'](?P<href>[^"\']*/lots/\?[^"\']*)["\'][^>]*>\s*View\s+Early\s+Consignments',
    re.I,
)


def parse_early_consignments_filters(auction_html: str) -> dict | None:
    """Extract ``filtersInput`` + ``sortBy`` from the 'View Early Consignments'
    button on an auction page. Returns ``None`` if the button is missing
    (past auctions / far-future auctions without published previews).
    """
    m = _EC_BUTTON_RE.search(auction_html or "")
    if not m:
        return None
    # ``href`` in the raw HTML uses ``&amp;`` between query-string pairs;
    # unescape so ``parse_qsl`` sees real ``&`` separators (otherwise only the
    # first pair is parsed and the venue/year filters are silently dropped).
    href = m.group("href").replace("&amp;", "&")
    qs = href.split("?", 1)[1] if "?" in href else ""
    pairs = urllib.parse.parse_qsl(qs, keep_blank_values=True)
    filters: dict[str, list[str]] = {}
    sort_by = "ENDING_SOONEST"
    for k, v in pairs:
        if k == "sortBy":
            sort_by = v or sort_by
            continue
        # ``filtersInput[liveAuctionVenue][0]`` → key='liveAuctionVenue'
        fm = re.match(r"filtersInput\[([^\]]+)\]\[\d+\]", k)
        if not fm:
            continue
        filters.setdefault(fm.group(1), []).append(v)
    if not filters:
        return None
    return {"filters": filters, "sort_by": sort_by}


@retry(**_RETRY)
async def fetch_upcoming_vehicles_graphql(
    client: httpx.AsyncClient,
    *,
    filters: dict[str, list[str]],
    sort_by: str = "ENDING_SOONEST",
    page_number: int = 0,
) -> list[dict[str, Any]]:
    """Replay the ``GetVehiclesAndFilters`` persisted query — returns the
    ``data.getVehicles.vehicles`` list for one page (typically 24 items)."""
    # The site always includes these empty arrays — keep them to maximise
    # cache-key parity with the live request.
    base_filters: dict[str, list[str]] = {
        "make": [],
        "auctionType": [],
        "itemType": [],
        "venue": [],
        "auctionYear": [],
        "liveAuctionVenue": [],
    }
    base_filters.update(filters)
    variables = {
        "filtersInput": base_filters,
        "sortBy": sort_by,
        "pageNumber": page_number,
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": GRAPHQL_PERSISTED_HASH,
        },
    }
    params = {
        "operationName": GRAPHQL_OPERATION,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(extensions, separators=(",", ":")),
    }
    headers = {
        **_COMMON_HEADERS,
        "Accept": "*/*",
        "Origin": SITE_ORIGIN,
        "Referer": SITE_ORIGIN + "/",
        # The live site sends a literal 'Bearer' (no token) — Apollo client
        # default. Reproducing it avoids any auth-check edge case.
        "Authorization": "Bearer",
    }
    r = await client.get(GRAPHQL_URL, params=params, headers=headers)
    r.raise_for_status()
    payload = r.json()
    return (((payload or {}).get("data") or {}).get("getVehicles") or {}).get("vehicles") or []


async def iter_upcoming_vehicles_graphql(
    client: httpx.AsyncClient,
    *,
    filters: dict[str, list[str]],
    sort_by: str = "ENDING_SOONEST",
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Page through GraphQL until an empty page (or ``max_pages``) is returned."""
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        chunk = await fetch_upcoming_vehicles_graphql(
            client, filters=filters, sort_by=sort_by, page_number=page,
        )
        if not chunk:
            break
        out.extend(chunk)
        # The site appears to page in groups of 24; an exact-multiple-of-N
        # short circuit isn't reliable, so we only stop on empty.
    return out
