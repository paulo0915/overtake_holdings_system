"""HTTP client — The MB Market AJAX list + listing HTML (Cloudflare-safe TLS)."""

from __future__ import annotations

import logging
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import AJAX_LIST_URL, BASE_URL, INITIAL_LAST_PAGE_PLACEHOLDER, RESULTS_REFERER

logger = logging.getLogger(__name__)

_TIMEOUT = float(REQUEST_TIMEOUT)


class MbMarketApiError(RuntimeError):
    """Raised when the listings AJAX call fails or returns unexpected JSON."""


def _ajax_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "referer": RESULTS_REFERER,
        "origin": BASE_URL,
        "x-requested-with": "XMLHttpRequest",
    }


def _build_form_data(
    *,
    limit: int,
    page: int,
    last_page: int,
    listing_type: str,
    search: str = "",
    chassis_code: str = "",
    sort_by: str = "",
) -> str:
    """Inner URL-encoded string posted as the ``form_data`` field."""
    return (
        f"limit={limit}&latitude=&longitude=&auction_page={page}"
        f"&auction_last_page={last_page}&type={listing_type}"
        f"&search={search}&chassis_code={chassis_code}&sort_by={sort_by}"
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(RequestsError),
)
async def fetch_auction_list_page(
    session: AsyncSession,
    *,
    page: int,
    last_page: int,
    limit: int,
    listing_type: str,
) -> dict[str, Any]:
    """
    POST one page of auction result cards.

    The site expects ``application/x-www-form-urlencoded`` with a single field
    ``form_data`` whose value is the inner query string (not flat POST keys).
    """
    inner = _build_form_data(limit=limit, page=page, last_page=last_page, listing_type=listing_type)
    r = await session.post(
        AJAX_LIST_URL,
        data={"form_data": inner},
        headers=_ajax_headers(),
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise MbMarketApiError(f"list HTTP {r.status_code}: {r.text[:300]}")
    try:
        payload = r.json()
    except Exception as e:
        raise MbMarketApiError(f"list JSON decode failed: {e}") from e
    if payload.get("status") != "success":
        raise MbMarketApiError(f"list status not success: {payload!r}"[:500])
    return payload


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(RequestsError),
)
async def fetch_listing_html(session: AsyncSession, slug_path: str) -> str | None:
    """GET ``/{slug_path}`` (leading slash optional)."""
    path = slug_path if slug_path.startswith("/") else f"/{slug_path}"
    url = f"{BASE_URL}{path}"
    r = await session.get(url, headers={"referer": RESULTS_REFERER}, timeout=_TIMEOUT)
    if r.status_code != 200:
        logger.warning("Detail %s returned HTTP %s", path, r.status_code)
        return None
    return r.text
