"""HTTP client — SBX Cars listing HTML (Cloudflare-friendly TLS)."""

from __future__ import annotations

import logging

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import BASE_URL, RESULTS_REFERER

logger = logging.getLogger(__name__)

_TIMEOUT = float(REQUEST_TIMEOUT)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(RequestsError),
)
async def fetch_results_html(session: AsyncSession) -> str | None:
    r = await session.get(
        f"{BASE_URL}/results",
        headers={"referer": RESULTS_REFERER},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        logger.warning("Results page HTTP %s", r.status_code)
        return None
    return r.text


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(RequestsError),
)
async def fetch_listing_html(session: AsyncSession, listing_path: str) -> str | None:
    """GET ``/listing/{id}/{slug}`` (path must start with ``/listing/``)."""
    path = listing_path if listing_path.startswith("/") else f"/{listing_path}"
    url = f"{BASE_URL}{path}"
    r = await session.get(url, headers={"referer": RESULTS_REFERER}, timeout=_TIMEOUT)
    if r.status_code != 200:
        logger.warning("Listing %s returned HTTP %s", path, r.status_code)
        return None
    return r.text
