"""HTTP helpers for Gooding & Company — homepage, app bundle, lot page-data JSON."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import APP_JS_HREF_RE, PAGE_DATA_PATH_TEMPLATE, SITE_ORIGIN

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_COMMON_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

_APP_JS_RE = re.compile(APP_JS_HREF_RE)


async def fetch_home_html(client: httpx.AsyncClient) -> str:
    r = await client.get(SITE_ORIGIN + "/", headers=_COMMON_HEADERS)
    r.raise_for_status()
    return r.text


def find_app_js_path(home_html: str) -> str | None:
    """Locate the current ``/app-*.js`` bundle path (hash changes per deploy)."""
    m = _APP_JS_RE.search(home_html)
    return m.group(0) if m else None


async def fetch_app_js(client: httpx.AsyncClient, app_js_path: str) -> str:
    url = SITE_ORIGIN + app_js_path
    r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.text


async def fetch_page_data(
    client: httpx.AsyncClient, *, path: str,
) -> dict[str, Any]:
    """Fetch ``/page-data/<path>/page-data.json`` for a lot or auction page."""
    url = SITE_ORIGIN + PAGE_DATA_PATH_TEMPLATE.format(path=path)
    r = await client.get(url, headers={**_COMMON_HEADERS, "Referer": SITE_ORIGIN + "/"})
    r.raise_for_status()
    return r.json()
