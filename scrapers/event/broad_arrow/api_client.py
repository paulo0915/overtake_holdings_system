"""HTTP fetch helpers for Broad Arrow — session-aware, event-based discovery."""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT, get_headers

from .config import PAST_AUCTIONS_PATH, RESULTS_PATH, SITE_ORIGIN, SORT_PARAM, SORT_VALUE

logger = logging.getLogger(__name__)

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=12),
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException)
    ),
)


@retry(**_RETRY)
async def fetch_warmup(client: httpx.AsyncClient) -> None:
    """GET site root to establish cookies and satisfy reCAPTCHA v3 session scoring."""
    resp = await client.get(
        SITE_ORIGIN + "/",
        headers=get_headers(accept="text/html"),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()


@retry(**_RETRY)
async def fetch_past_auctions_html(client: httpx.AsyncClient) -> str:
    """GET /past-auctions — lists every auction event."""
    resp = await client.get(
        SITE_ORIGIN + PAST_AUCTIONS_PATH,
        headers=get_headers(accept="text/html", referer=SITE_ORIGIN + "/"),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


@retry(**_RETRY)
async def fetch_event_html(client: httpx.AsyncClient, event_path: str) -> str:
    """GET /events/event/{name} — contains branch-specific results URL."""
    url = SITE_ORIGIN + event_path if event_path.startswith("/") else event_path
    resp = await client.get(
        url,
        headers=get_headers(
            accept="text/html", referer=SITE_ORIGIN + PAST_AUCTIONS_PATH
        ),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


@retry(**_RETRY)
async def fetch_event_meta_html(client: httpx.AsyncClient, slug: str) -> str:
    """GET /events/event/{slug} — used to extract location/date metadata per event."""
    url = f"{SITE_ORIGIN}/events/event/{slug}/"
    resp = await client.get(
        url,
        headers=get_headers(
            accept="text/html", referer=SITE_ORIGIN + PAST_AUCTIONS_PATH
        ),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


@retry(**_RETRY)
async def fetch_branch_results_html(
    client: httpx.AsyncClient, *, url: str, event_url: str
) -> str:
    """GET a paginated branch results page (vehicle card listing).

    ``url``       — full URL already containing ``branch_id_eq`` + ``page``
    ``event_url`` — used as Referer so the request looks like natural navigation
    """
    resp = await client.get(
        url,
        headers=get_headers(accept="text/html", referer=event_url),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


@retry(**_RETRY)
async def fetch_upcoming_vehicles_html(
    client: httpx.AsyncClient, *, page: int = 1,
) -> str:
    """GET ``/vehicles?page=N`` — default listing shows only upcoming auctions."""
    from .config import UPCOMING_LISTING_PATH

    url = f"{SITE_ORIGIN}{UPCOMING_LISTING_PATH}"
    if page > 1:
        url = f"{url}?page={page}"
    resp = await client.get(
        url,
        headers=get_headers(accept="text/html", referer=SITE_ORIGIN + "/"),
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


async def fetch_vehicle_html(
    client: httpx.AsyncClient, *, vehicle_path: str, referer: str = ""
) -> str | None:
    """GET /vehicles/{auction_lot}/{slug} detail page — returns None on any error."""
    path = vehicle_path.strip().lstrip("/")
    if not path.startswith("vehicles/"):
        path = f"vehicles/{path}"
    url = f"{SITE_ORIGIN}/{path}"
    try:
        resp = await client.get(
            url,
            headers=get_headers(
                accept="text/html",
                referer=referer or SITE_ORIGIN + RESULTS_PATH,
            ),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning("Vehicle page fetch failed %s: %s", url, e)
        return None


def branch_results_page_url(branch_results_base: str, page: int) -> str:
    """Append ``&page=N`` to the branch results base URL."""
    sep = "&" if "?" in branch_results_base else "?"
    return f"{branch_results_base}{sep}page={page}"
