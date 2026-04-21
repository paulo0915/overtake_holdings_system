"""HTTP client for Barrett-Jackson previous-docket JSON API."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT, get_headers

from .config import RESULTS_API_PATH, SITE_ORIGIN

logger = logging.getLogger(__name__)

# Matches direct event-page hrefs: href="/2026-columbus" on the /auctions schedule page.
# City names are 1–3 lowercase-alpha words; no digits in the city portion.
_EVENT_HREF_RE = re.compile(
    r'href=["\']/(20\d\d-(?:[a-z]+(?:-[a-z]+){0,2}))["\']',
)

AUCTIONS_SCHEDULE_PATH = "/auctions"


def _results_query_params(
    *, page: int, page_size: int, sort: str, result_type: str, non_included_slugs: str,
    event_slug_filter: str = "",
) -> dict[str, str]:
    """Mirror the browser query string (empty filters = full universe minus ``non_included``)."""
    return {
        "page": str(page),
        "type": result_type,
        "size": str(page_size),
        "slug": event_slug_filter,
        "q": "",
        "sort": sort,
        "make": "",
        "year": "",
        "is_reserve": "",
        "collection": "",
        "style": "",
        "dayOfWeek": "",
        "lot": "",
        "model": "",
        "engine_size": "",
        "interior_color": "",
        "exterior_color": "",
        "transmission_type_name": "",
        "number_of_cylinders": "",
        "nonIncludedSlugs": non_included_slugs,
    }


async def discover_non_included_slugs(client: httpx.AsyncClient) -> str:
    """Auto-detect upcoming/open-docket events that must be excluded from the sold index.

    Strategy
    --------
    1. Fetch ``/auctions`` — the Barrett-Jackson schedule page — and extract anchor
       ``href`` links matching ``/YYYY-{city}`` (e.g. ``/2026-columbus``). This page
       is the canonical public schedule and lists all upcoming events.
    2. For each candidate, query ``/api/previous-docket-results?slug={candidate}&size=1``.
       If the API returns **zero total hits**, the event has no sold results yet — it is
       upcoming or in progress and should be in ``nonIncludedSlugs``.
    3. Return a comma-separated string suitable for the ``nonIncludedSlugs`` query param.

    Falls back to an empty string (no exclusions) on any network or parse error — the
    scraper continues normally; at worst a handful of preview/open lots may appear.
    """
    schedule_url = f"{SITE_ORIGIN}{AUCTIONS_SCHEDULE_PATH}"
    try:
        html_headers = get_headers(accept="text/html", referer=SITE_ORIGIN)
        resp = await client.get(schedule_url, headers=html_headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("BJ /auctions fetch failed during slug discovery: %s", exc)
        return ""

    current_year = date.today().year
    candidates: list[str] = []
    for m in _EVENT_HREF_RE.finditer(html):
        slug = m.group(1)
        year_str = slug.split("-")[0]
        if not year_str.isdigit():
            continue
        yr = int(year_str)
        # Only consider slugs for the current or next year (older events are already in the index).
        if yr < current_year or yr > current_year + 1:
            continue
        if slug not in candidates:
            candidates.append(slug)

    if not candidates:
        logger.info("BJ slug discovery: no upcoming events found on /auctions schedule page")
        return ""

    logger.info("BJ slug discovery: %s candidate(s) from schedule: %s", len(candidates), candidates)

    json_headers = get_headers(
        accept="application/json",
        referer=f"{SITE_ORIGIN}/results?type=Vehicles",
    )
    json_headers["Origin"] = SITE_ORIGIN

    upcoming: list[str] = []
    for slug in candidates:
        params = _results_query_params(
            page=1, page_size=1, sort="price_desc", result_type="Vehicles",
            non_included_slugs="", event_slug_filter=slug,
        )
        url = f"{SITE_ORIGIN}{RESULTS_API_PATH}?{urlencode(params)}"
        try:
            r = await client.get(url, headers=json_headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            total = int(
                (r.json().get("meta") or {}).get("pagination", {}).get("total") or 0
            )
        except Exception as exc:
            logger.warning("BJ slug discovery: check for %r failed: %s", slug, exc)
            continue
        if total == 0:
            upcoming.append(slug)
            logger.info("BJ slug discovery: %r has 0 sold results -> excluding (open docket)", slug)
        else:
            logger.info("BJ slug discovery: %r has %s sold results -> including normally", slug, total)

    result = ",".join(upcoming)
    logger.info("BJ slug discovery: nonIncludedSlugs = %r", result)
    return result


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)),
)
async def fetch_previous_docket_page(
    client: httpx.AsyncClient,
    *,
    page: int,
    page_size: int,
    sort: str,
    result_type: str,
    non_included_slugs: str,
) -> dict[str, Any]:
    """One page of ``/api/previous-docket-results`` (``data`` + ``meta``)."""
    params = _results_query_params(
        page=page,
        page_size=page_size,
        sort=sort,
        result_type=result_type,
        non_included_slugs=non_included_slugs,
    )
    url = f"{SITE_ORIGIN}{RESULTS_API_PATH}?{urlencode(params)}"
    headers = get_headers(
        accept="application/json",
        referer=f"{SITE_ORIGIN}/results?type={result_type}&slug=&page={page}&size={page_size}&sorting={sort}",
    )
    headers["Origin"] = SITE_ORIGIN
    resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or "data" not in data:
        raise RuntimeError(f"Unexpected BJ API response shape on page {page}")
    return data
