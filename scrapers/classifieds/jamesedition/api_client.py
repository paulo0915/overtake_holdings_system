"""HTTP helpers for JamesEdition — curl_cffi with residential proxy rotation.

Cloudflare's managed challenge blocks direct requests and headless browsers, but
passes residential proxy IPs cleanly. We rotate through the shared iProyal proxy
pool (same pool used by autotrader_com) with curl_cffi Chrome impersonation.
"""

from __future__ import annotations

import logging

from curl_cffi.requests import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.proxies import get_random_proxy, mark_bad

from .config import LIST_PATH, SITE_ORIGIN, SORT_PARAM_NAME, SORT_PRICE_DESC

logger = logging.getLogger(__name__)

IMPERSONATE = "chrome124"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{SITE_ORIGIN}/cars",
}


def _is_cf_challenge(html: str) -> bool:
    return not html or (len(html) < 50_000 and "just a moment" in html.lower())


class JESession:
    """curl_cffi session with a residential proxy that bypasses Cloudflare."""

    def __init__(self) -> None:
        self._session: AsyncSession | None = None
        self._proxy_url: str | None = None

    async def start(self) -> None:
        proxy = get_random_proxy()
        self._proxy_url = proxy.url if proxy else None
        proxies = {"https": self._proxy_url, "http": self._proxy_url} if self._proxy_url else {}
        self._session = AsyncSession(impersonate=IMPERSONATE, timeout=45, proxies=proxies)
        logger.info("JESession: proxy=%s", self._proxy_url[:40] + "..." if self._proxy_url else "none")

    async def fetch_list_page(self, *, page: int) -> str:
        url = f"{SITE_ORIGIN}{LIST_PATH}?{SORT_PARAM_NAME}={SORT_PRICE_DESC}&page={page}"
        r = await self._session.get(url, headers=_HEADERS)
        html = r.text
        if _is_cf_challenge(html):
            if self._proxy_url:
                mark_bad(self._proxy_url)
            raise RuntimeError(f"CF challenge on page {page} (status={r.status_code})")
        return html

    async def fetch_detail_html(self, *, url: str) -> str | None:
        r = await self._session.get(url, headers=_HEADERS)
        if r.status_code == 404:
            return None
        html = r.text
        if _is_cf_challenge(html):
            return None
        return html

    async def rotate_proxy(self) -> None:
        """Swap to a fresh proxy to avoid per-IP rate limiting mid-detail-fetch."""
        if self._session:
            await self._session.close()
        proxy = get_random_proxy()
        self._proxy_url = proxy.url if proxy else None
        proxies = {"https": self._proxy_url, "http": self._proxy_url} if self._proxy_url else {}
        self._session = AsyncSession(impersonate=IMPERSONATE, timeout=45, proxies=proxies)
        logger.info(
            "JESession: rotated proxy -> %s",
            self._proxy_url[:40] + "..." if self._proxy_url else "none",
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()
