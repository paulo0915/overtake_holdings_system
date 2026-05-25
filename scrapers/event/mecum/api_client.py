"""Algolia multi-query API + Mecum lot HTML."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT, get_headers

from .config import (
    ALGOLIA_AGENT,
    ALGOLIA_API_KEY,
    ALGOLIA_APP_ID,
    ALGOLIA_QUERIES_URL,
    AUCTIONS_OVERVIEW_PATH,
    HITS_PER_PAGE,
    LOT_INDEX_NAME,
    MECUM_SITE_ORIGIN,
    UPCOMING_LOT_INDEX_NAME,
    run_date_timestamp_ceiling,
)

logger = logging.getLogger(__name__)


class MecumApiError(Exception):
    """Algolia or HTTP failure for Mecum."""


def _algolia_query_params() -> dict[str, str]:
    return {
        "x-algolia-agent": ALGOLIA_AGENT,
        "x-algolia-api-key": ALGOLIA_API_KEY,
        "x-algolia-application-id": ALGOLIA_APP_ID,
    }


def _sold_lots_request_body(page: int) -> dict[str, Any]:
    ceiling = run_date_timestamp_ceiling()
    filters = f"taxonomies.run_date.timestamp<{ceiling} OR sold=1 OR bid_goes_on=1"
    facets = [
        "engine_configuration_meta",
        "generic_exterior_color_meta",
        "generic_interior_color_meta",
        "taxonomies.auction_tax.name",
        "taxonomies.body_type.name",
        "taxonomies.division_tax.name",
        "taxonomies.lot_status.name",
        "taxonomies.lot_type.name",
        "taxonomies.lot_year.name",
        "taxonomies.make.name",
        "taxonomies.model.name",
        "taxonomies.run_date.timestamp",
        "taxonomies.sale_result.name",
        "transmission_type_meta",
    ]
    return {
        "requests": [
            {
                "indexName": LOT_INDEX_NAME,
                "facetFilters": [["taxonomies.sale_result.name:sold"]],
                "facets": facets,
                "filters": filters,
                "highlightPostTag": "__/ais-highlight__",
                "highlightPreTag": "__ais-highlight__",
                "hitsPerPage": HITS_PER_PAGE,
                "maxValuesPerFacet": 1000,
                "page": page,
                "query": "",
                "ruleContexts": ["pin_items"],
            },
            # Companion facet-count query that the browser sends alongside the
            # main hits query (Algolia InstantSearch pattern). Mirroring the
            # real DevTools payload keeps the request shape identical to a
            # genuine browser session, which makes the price-sorted hit order
            # in the first response stable across pages.
            {
                "indexName": LOT_INDEX_NAME,
                "analytics": False,
                "clickAnalytics": False,
                "facets": "taxonomies.sale_result.name",
                "filters": filters,
                "highlightPostTag": "__/ais-highlight__",
                "highlightPreTag": "__ais-highlight__",
                "hitsPerPage": 0,
                "maxValuesPerFacet": 1000,
                "page": 0,
                "query": "",
                "ruleContexts": ["pin_items"],
            },
        ],
    }


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
)
async def fetch_sold_lots_page(client: httpx.AsyncClient, *, page: int) -> dict[str, Any]:
    """One Algolia page of sold lots (``wp_posts_lot_highest_bid_desc``)."""
    payload = _sold_lots_request_body(page)
    h = get_headers(accept="application/json", referer=f"{MECUM_SITE_ORIGIN}/")
    h["Content-Type"] = "application/json"
    h["Origin"] = MECUM_SITE_ORIGIN
    resp = await client.post(
        ALGOLIA_QUERIES_URL,
        params=_algolia_query_params(),
        json=payload,
        headers=h,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise MecumApiError("Algolia response missing results[]")
    first = results[0]
    if isinstance(first, dict) and first.get("message"):
        raise MecumApiError(str(first.get("message")))
    return first


# --- Upcoming-auction support ---------------------------------------------

# Matches "View Lots" anchors on /auctions/ — only auctions whose catalog
# is publicly browsable render this link. Far-future auctions show a plain
# date card without a /lots/ href, which this regex naturally skips.
_UPCOMING_LOTS_HREF_RE = re.compile(
    r'href=["\']/auctions/([a-z0-9]+(?:-[a-z0-9]+)+)/lots/?["\']',
    re.I,
)


async def discover_upcoming_auction_slugs(client: httpx.AsyncClient) -> list[str]:
    """Scrape /auctions/ for ``/auctions/{slug}/lots/`` hrefs.

    Returns slugs in page order (typically chronological). Dedupes.
    Returns ``[]`` on any network failure rather than crashing the scrape —
    caller logs and proceeds with no upcoming events.
    """
    url = f"{MECUM_SITE_ORIGIN}{AUCTIONS_OVERVIEW_PATH}"
    try:
        resp = await client.get(
            url,
            headers=get_headers(
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                referer=MECUM_SITE_ORIGIN + "/",
            ),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Mecum upcoming discovery: /auctions fetch failed: %s", e)
        return []
    seen: set[str] = set()
    slugs: list[str] = []
    for m in _UPCOMING_LOTS_HREF_RE.finditer(resp.text):
        slug = m.group(1).lower()
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    logger.info(
        "Mecum upcoming discovery: %s event(s) with published lot catalog: %s",
        len(slugs), slugs,
    )
    return slugs


def _slugify_auction_title(title: str) -> str:
    """``'Tulsa 2026'`` → ``'tulsa-2026'``. Matches the URL slug convention."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _facet_names_request_body() -> dict[str, Any]:
    """One Algolia query that returns ZERO hits but ALL distinct
    ``auction_tax.name`` facet values. We need the full name (with the
    pipe-delimited start/end timestamps) to filter per-auction queries."""
    return {
        "requests": [
            {
                "indexName": UPCOMING_LOT_INDEX_NAME,
                "facets": ["taxonomies.auction_tax.name"],
                "filters": "",
                "hitsPerPage": 0,
                "maxValuesPerFacet": 1000,
                "page": 0,
                "query": "",
            },
        ],
    }


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
)
async def fetch_upcoming_auction_facet_names(
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """Return ``{slug: full_auction_tax_name}`` for every auction Algolia knows.

    ``full_auction_tax_name`` is the pipe-delimited string Algolia indexes,
    e.g. ``"Tulsa 2026|1780617600|1780704000"`` — required verbatim for
    per-auction filter queries. We slugify the title prefix to map back
    to the URL slug discovered from /auctions/.
    """
    payload = _facet_names_request_body()
    h = get_headers(accept="application/json", referer=f"{MECUM_SITE_ORIGIN}/")
    h["Content-Type"] = "application/json"
    h["Origin"] = MECUM_SITE_ORIGIN
    resp = await client.post(
        ALGOLIA_QUERIES_URL,
        params=_algolia_query_params(),
        json=payload,
        headers=h,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("results") or [{}])[0]
    facets = (results.get("facets") or {}).get("taxonomies.auction_tax.name") or {}
    out: dict[str, str] = {}
    for full_name in facets:
        title = full_name.split("|", 1)[0].strip()
        if not title:
            continue
        slug = _slugify_auction_title(title)
        out[slug] = full_name
    return out


def _upcoming_lots_request_body(*, auction_name: str, page: int) -> dict[str, Any]:
    """Paginated Algolia query for one upcoming auction's lots."""
    # Algolia filters need quoted values; escape any embedded quote.
    safe = auction_name.replace('"', '\\"')
    return {
        "requests": [
            {
                "indexName": UPCOMING_LOT_INDEX_NAME,
                "facets": ["taxonomies.auction_tax.name"],
                "filters": f'taxonomies.auction_tax.name:"{safe}"',
                "highlightPostTag": "__/ais-highlight__",
                "highlightPreTag": "__ais-highlight__",
                "hitsPerPage": HITS_PER_PAGE,
                "maxValuesPerFacet": 50,
                "page": page,
                "query": "",
            },
        ],
    }


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
)
async def fetch_upcoming_lots_page(
    client: httpx.AsyncClient,
    *,
    auction_name: str,
    page: int,
) -> dict[str, Any]:
    """One Algolia page of upcoming lots scoped to a single auction."""
    payload = _upcoming_lots_request_body(auction_name=auction_name, page=page)
    h = get_headers(accept="application/json", referer=f"{MECUM_SITE_ORIGIN}/")
    h["Content-Type"] = "application/json"
    h["Origin"] = MECUM_SITE_ORIGIN
    resp = await client.post(
        ALGOLIA_QUERIES_URL,
        params=_algolia_query_params(),
        json=payload,
        headers=h,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise MecumApiError("Algolia upcoming response missing results[]")
    first = results[0]
    if isinstance(first, dict) and first.get("message"):
        raise MecumApiError(str(first.get("message")))
    return first


# --- Per-lot HTML (shared between past and upcoming) ----------------------


async def fetch_lot_html(client: httpx.AsyncClient, *, path: str) -> str | None:
    """GET lot detail HTML (embeds ``__NEXT_DATA__`` with ``pageProps.post``)."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{MECUM_SITE_ORIGIN}{path}"
    try:
        resp = await client.get(
            url,
            headers=get_headers(
                accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                referer=f"{MECUM_SITE_ORIGIN}/",
            ),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning("Lot page fetch failed %s: %s", url, e)
        return None
