"""HTTP — curl_cffi (Chrome TLS) for listings subdomain."""

from __future__ import annotations

import logging
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.config import REQUEST_TIMEOUT

from .config import CAR_SEARCH_PATH, INVENTORY_PATH, LISTINGS_ORIGIN, MAIN_ORIGIN, RESULTS_PATH

logger = logging.getLogger(__name__)

_TIMEOUT = float(REQUEST_TIMEOUT)

_RETRY = dict(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(RequestsError),
)


@retry(**_RETRY)
async def fetch_text(session: AsyncSession, url: str, *, referer: str = "") -> str:
    headers: dict[str, str] = {}
    if referer:
        headers["referer"] = referer
    r = await session.get(url, headers=headers or None, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RequestsError(f"GET {url} HTTP {r.status_code}")
    return r.text


@retry(**_RETRY)
async def post_form(
    session: AsyncSession, url: str, data: dict[str, str], *, referer: str, origin: str
) -> str:
    r = await session.post(
        url,
        data=data,
        headers={
            "referer": referer,
            "origin": origin,
            "content-type": "application/x-www-form-urlencoded",
        },
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise RequestsError(f"POST {url} HTTP {r.status_code}")
    return r.text


async def warmup(session: AsyncSession) -> None:
    """Establish cookies like a browser visiting the main site then listings."""
    await session.get(MAIN_ORIGIN + "/", timeout=_TIMEOUT)
    await session.get(LISTINGS_ORIGIN + "/", headers={"referer": MAIN_ORIGIN + "/"}, timeout=_TIMEOUT)


async def fetch_results_html(session: AsyncSession) -> str:
    url = LISTINGS_ORIGIN + RESULTS_PATH
    return await fetch_text(session, url, referer=LISTINGS_ORIGIN + "/")


async def post_car_search(session: AsyncSession, form: dict[str, str]) -> str:
    """POST the car-search form (mirrors the Results page search)."""
    url = LISTINGS_ORIGIN + CAR_SEARCH_PATH
    return await post_form(
        session,
        url,
        form,
        referer=LISTINGS_ORIGIN + RESULTS_PATH,
        origin=LISTINGS_ORIGIN,
    )


async def fetch_inventory_html(session: AsyncSession) -> str:
    """Full inventory grid (WooCommerce product cards) — one HTML page."""
    url = LISTINGS_ORIGIN + INVENTORY_PATH
    return await fetch_text(
        session,
        url,
        referer=LISTINGS_ORIGIN + CAR_SEARCH_PATH,
    )


async def fetch_product_html(session: AsyncSession, product_url: str) -> str | None:
    """GET a single ``/product/.../`` page. Returns ``None`` on failure."""
    try:
        r = await session.get(
            product_url,
            headers={"referer": LISTINGS_ORIGIN + INVENTORY_PATH},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("Product %s HTTP %s", product_url, r.status_code)
            return None
        return r.text
    except RequestsError as e:
        logger.warning("Product fetch failed %s: %s", product_url, e)
        return None
