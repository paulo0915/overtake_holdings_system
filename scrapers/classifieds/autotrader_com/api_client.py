"""HTTP helpers for AutoTrader.com — Playwright Firefox with in-page fetch.

Architecture:
    1. Launch headless Firefox through a residential proxy
    2. Warmup: load homepage, let Akamai's sensor JS write cookies
    3. Call ``/rest/lsc/listing`` via ``page.evaluate('fetch(...)')`` so
       the request goes through the browser's real JS fetch stack —
       same cookies, same TLS fingerprint, same referer context that
       earned ``_abck``.
    4. Detail pages via the same in-page fetch (reads HTML for VDP).

Why in-page ``fetch()`` (not ``curl_cffi``, not ``page.request.get()``):

    Empirically verified (2026-04): on a clean non-US residential IP,
    calling the REST endpoint via ``page.request.get()`` (Playwright's
    API client) or curl_cffi with ``firefox147`` impersonation both
    return Akamai's 4 KB "page unavailable" stub. The ONLY path that
    returns real JSON is ``page.evaluate('fetch(...)')`` — this runs
    in the actual page's JS context, so Akamai's per-request sensor
    validation sees the same fingerprint + cookie state that earned
    ``_abck`` on the homepage.

    Tradeoff: slower than curl_cffi (browser stays alive for the whole
    run), but it's the only pattern that actually works. Typical
    throughput: ~2-4 requests/sec, fine for thousands of listings.

Non-US proxies only:
    iProyal's US residential pool is hard-blocked by Akamai regardless
    of stealth. JP / PL / PH exits pass the IP-reputation gate at
    ~20-30% success rate per session. Caller's retry loop iterates
    until one works.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from shared.proxies import ProxyInfo

from .config import (
    ALL_LISTING_TYPE,
    DEFAULT_CITY,
    DEFAULT_PAGE_SIZE,
    DEFAULT_STATE,
    DEFAULT_ZIPCODE,
    LIST_REST_PATH,
    SITE_ORIGIN,
    listing_type_for_mode,
)

logger = logging.getLogger(__name__)


class AkamaiChallengeError(RuntimeError):
    """Akamai served the 4 KB "page unavailable" stub or a challenge
    response — this session can't proceed. Recoverable by retrying
    with a fresh proxy; not a proxy-level failure (no connection error).
    """


# --- Session wrapper holding the live Playwright browser -------------------

class AutoTraderSession:
    """One long-lived Playwright Firefox session bound to a single proxy.

    The browser must stay alive for the duration of the scrape — Akamai
    ties ``_abck`` to the client instance. Closing the browser and
    re-opening with the same cookies earns a fresh challenge.

    Exposes three coroutines for the scraper to call:
        ``fetch_list_page(mode, first_record)`` → parsed JSON dict
        ``fetch_detail_html(listing_id)`` → HTML string or None
        ``close()`` → tear down the browser

    All HTTP goes through ``page.evaluate('fetch(...)')`` so requests
    share the exact browser state Akamai validated during warmup.
    """

    def __init__(self, proxy: ProxyInfo | None = None) -> None:
        self.proxy = proxy
        self._playwright: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None
        # Dedicated single-worker executor. Playwright's sync_api binds
        # the instance to the thread that called ``sync_playwright().start()``;
        # every subsequent call (``page.goto``, ``page.evaluate``, etc.)
        # MUST come from that same thread or Playwright raises
        # ``Cannot switch to a different thread``. The default
        # ``asyncio.to_thread`` uses a multi-worker pool, so two list-page
        # fetches issued back-to-back could land on different workers and
        # crash. ``max_workers=1`` makes the whole session pin to one OS
        # thread for its lifetime.
        from concurrent.futures import ThreadPoolExecutor
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"atcom-pw-{id(self) & 0xFFFF:x}",
        )

    async def _run_sync(self, fn, /, *args, **kwargs):
        """Schedule ``fn(*args, **kwargs)`` on this session's dedicated
        Playwright thread and await the result. Replaces ``asyncio.to_thread``
        which uses a multi-worker pool and breaks Playwright's thread binding.
        """
        import asyncio as _aio
        loop = _aio.get_running_loop()
        if kwargs:
            from functools import partial
            return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))
        return await loop.run_in_executor(self._executor, fn, *args)

    # --- Sync Playwright lifecycle must run off the asyncio loop. ----
    # Each public async method schedules the sync work on the session's
    # dedicated thread (see ``_run_sync``).

    async def warmup(self) -> None:
        """Launch Firefox, load homepage, harvest Akamai cookies.

        Raises ``AkamaiChallengeError`` if Akamai serves the
        interstitial (proxy IP burned) or if critical cookies never set.
        """
        await self._run_sync(self._sync_warmup)

    def _sync_warmup(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = dict(headless=True)
        if self.proxy is not None:
            launch_kwargs["proxy"] = self.proxy.playwright_dict
        self._browser = self._playwright.firefox.launch(**launch_kwargs)
        self._ctx = self._browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1920, "height": 1080},
        )
        self._page = self._ctx.new_page()

        self._page.goto(
            f"{SITE_ORIGIN}/",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        home_size = len(self._page.content())
        home_title = self._page.title()
        logger.info("warmup: home size=%s title=%r", home_size, home_title)
        if home_size < 50_000 or "page unavailable" in home_title.lower():
            self._sync_close()
            raise AkamaiChallengeError(
                f"Homepage hard-blocked (size={home_size} title={home_title!r}). "
                "Proxy IP is in Akamai's repeat-offender list."
            )

        # Sensor POST needs a few seconds + some mouse telemetry.
        try:
            self._page.mouse.move(400, 300)
            self._page.mouse.move(900, 500, steps=12)
            self._page.evaluate("window.scrollBy(0, 400)")
        except Exception:
            pass
        for i in range(8):
            self._page.wait_for_timeout(1_000)
            try:
                self._page.mouse.move(
                    500 + (i * 7) % 200, 400 + (i * 11) % 150, steps=3,
                )
            except Exception:
                pass

        cookies = self._ctx.cookies()
        cookie_names = {c["name"] for c in cookies}
        logger.info(
            "warmup: %s cookies set (critical: %s)",
            len(cookies),
            ", ".join(sorted(n for n in cookie_names if n.startswith(("_abck", "bm_", "ak_", "ATC")))),
        )
        needed = {"_abck", "bm_sz"}
        missing = needed - cookie_names
        if missing:
            self._sync_close()
            raise AkamaiChallengeError(
                f"Homepage loaded but missing required cookies: {missing}. "
                "Akamai's sensor JS didn't run or was rejected."
            )

        # Probe the REST endpoint to verify Akamai actually honors these
        # cookies for the target API — not just the homepage. Some sessions
        # pass the homepage gate but the REST path is challenged separately
        # (the IP can reach /cars-for-sale but not /rest/lsc/listing).
        # Caller retries with a new proxy when this fires.
        probe_url = (
            f"{SITE_ORIGIN}{LIST_REST_PATH}"
            "?newSearch=true&searchRadius=0&sortBy=derivedpriceDESC"
            f"&zip={DEFAULT_ZIPCODE}&allListingType={ALL_LISTING_TYPE}"
            f"&channel=ATC&firstRecord=0&numRecords=1"
        )
        probe_body = self._sync_fetch_blocking(probe_url)
        if _is_akamai_stub(probe_body):
            logger.info(
                "warmup REST probe rejected (size=%s), closing session",
                len(probe_body),
            )
            self._sync_close()
            raise AkamaiChallengeError(
                f"REST probe blocked after warmup (size={len(probe_body)}). "
                "Session can reach homepage but not the REST API."
            )
        try:
            data = json.loads(probe_body)
            if data.get("totalResultCount") is None and not data.get("listings"):
                logger.warning(
                    "REST probe returned unexpected JSON shape: keys=%s",
                    list(data.keys())[:10],
                )
        except json.JSONDecodeError:
            self._sync_close()
            raise AkamaiChallengeError(
                f"REST probe returned non-JSON (size={len(probe_body)})."
            )
        logger.info("warmup REST probe OK (total=%s)", data.get("totalResultCount"))

    def _sync_fetch_blocking(self, url: str) -> str:
        """Synchronous variant of the in-page fetch — used by the sync
        warmup path where we're already inside the Playwright thread.
        """
        return self._sync_fetch(url)

    async def fetch_list_page(
        self,
        *,
        mode: str,
        first_record: int,
        zip_code: str | None = None,
        city: str | None = None,
        state: str | None = None,
        radius: int | None = None,
        make_code: str | None = None,
    ) -> dict[str, Any]:
        """Call ``/rest/lsc/listing`` via in-page fetch. Returns the
        parsed JSON body. Raises ``AkamaiChallengeError`` if Akamai
        returns the challenge stub mid-run.

        ``zip_code``/``city``/``state``/``radius`` allow geo-sharded
        searches. ``make_code`` (e.g. ``"FERR"``, ``"LAM"``) restricts
        the query to one manufacturer — used by the make-shard phase
        to surface dealer inventory the geo phase misses. When omitted
        these fall back to ``DEFAULT_*`` (Ashburn VA with national mode
        and no make filter) so single-zip behavior is unchanged.
        """
        params: dict[str, str] = {
            "newSearch": "true",
            # radius=None means use the default behaviour (0 = national);
            # any positive integer caps to that mile radius around ``zip``.
            "searchRadius": str(radius) if radius is not None else "0",
            "sortBy": "derivedpriceDESC",
            "zip": zip_code or DEFAULT_ZIPCODE,
            "city": city or DEFAULT_CITY,
            "state": state or DEFAULT_STATE,
            "channel": "ATC",
            "relevanceConfig": "relevance-v2",
            "firstRecord": str(first_record),
            "numRecords": str(DEFAULT_PAGE_SIZE),
        }
        if make_code:
            params["makeCodeList"] = make_code
        lt = listing_type_for_mode(mode)
        if lt:
            params["listingTypes"] = lt
        else:
            params["allListingType"] = ALL_LISTING_TYPE

        from urllib.parse import urlencode
        url = f"{SITE_ORIGIN}{LIST_REST_PATH}?{urlencode(params)}"

        body = await self._run_sync(self._sync_fetch, url)
        if _is_akamai_stub(body):
            raise AkamaiChallengeError(
                f"Akamai served challenge stub on list call (size={len(body)})."
            )
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"List JSON parse failed ({e}); body[:200]={body[:200]!r}"
            ) from e

    async def fetch_detail_html(self, *, url: str) -> str | None:
        """Fetch a VDP via in-page ``fetch()``. Returns HTML or None on
        404 / challenge stub (caller decides whether to retry).
        """
        body = await self._run_sync(self._sync_fetch, url)
        if not body or _is_akamai_stub(body):
            return None
        return body

    async def fetch_detail_rendered(self, *, url: str) -> str | None:
        """Navigate to the VDP and return the **rendered** HTML.

        Slower than ``fetch_detail_html`` (full page.goto + hydration)
        but captures fields that Autotrader writes client-side after the
        page mounts — some listings have empty ``fullDescription`` in
        the SSR state but populate it via XHR after render, and the
        "More from the seller" DOM block ends up with content.

        Use as a fallback when the raw HTML fetch returned a short /
        empty description.
        """
        return await self._run_sync(self._sync_render, url)

    def _sync_render(self, url: str) -> str | None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            logger.debug("render goto failed for %s: %s", url, e)
            return None
        # Let client-side XHRs populate lazy fields + scroll to trigger
        # any intersection-observer-bound content.
        try:
            self._page.wait_for_timeout(3_000)
            for _ in range(4):
                self._page.evaluate("window.scrollBy(0, 600)")
                self._page.wait_for_timeout(400)
            self._page.wait_for_timeout(2_000)
            rendered = self._page.content()
        except Exception as e:
            logger.debug("render scroll/content failed for %s: %s", url, e)
            return None
        if _is_akamai_stub(rendered):
            return None
        return rendered

    def _sync_fetch(self, url: str) -> str:
        """Execute ``fetch(url)`` inside the page context. Returns the
        response text body. Empty string on JS-side errors.
        """
        js = """async (url) => {
            const r = await fetch(url, {
                credentials: 'include',
                headers: { 'Accept': '*/*' },
            });
            return await r.text();
        }"""
        try:
            return self._page.evaluate(js, url)
        except Exception as e:
            logger.warning("in-page fetch error: %s", e)
            return ""

    async def close(self) -> None:
        try:
            await self._run_sync(self._sync_close)
        finally:
            # Shut down the dedicated executor so the thread doesn't linger
            # after the session is discarded — critical when we rotate
            # sessions mid-run (otherwise every rotation leaks a thread).
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _sync_close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._ctx = None
        self._page = None
        self._playwright = None


def _is_akamai_stub(body: str) -> bool:
    """The 4 KB "page unavailable" interstitial has a consistent shape —
    empty body, very small size, or the title string. Used to bail
    early on per-request challenges.
    """
    if not body:
        return True
    if len(body) < 6_000 and "page unavailable" in body.lower():
        return True
    return False
