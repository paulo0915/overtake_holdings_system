"""AutoTrader.com — classifieds scraper orchestrator (used/new/all modes).

Architecture (browser-only, no curl_cffi):
    0) Try residential proxies from Supabase, prefer JP/PL/PH exits
       (US residentials are burned for Akamai).
    1) For each candidate: spin up headless Firefox, load homepage,
       let Akamai's sensor JS write ``_abck`` + ``bm_sz``.
    2) If warmup passes, keep the browser alive for the whole scrape.
       All REST + VDP calls go through ``page.evaluate('fetch(...)')``
       so every request shares the exact fingerprint + cookies that
       earned ``_abck``.
    3) If Akamai mid-run-challenges us (returns the 4 KB stub), tear
       down the browser and retry with a different proxy — continuing
       from the last successful offset.

``--mode used|new|all`` picks ``listingTypes``. ``all`` = mixed feed.
``--max-pages N`` = max list pages (25 listings each; site cap ~10k).
``--min-price USD`` = stop once the DESC-sorted price drops below.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS
from shared.export import export_to_csv, export_to_excel
from shared.normalizer import get_scraped_at
from shared.proxies import ProxyInfo, iter_available_proxies, mark_bad
from shared.schemas import CLASSIFIEDS_COLUMNS
from shared.vehicle_filter import VehicleFilter

from . import record_builder
from .api_client import AkamaiChallengeError, AutoTraderSession
from .config import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SHARD_RADIUS_MI,
    HARD_RESULT_CAP,
    LUXURY_MAKE_CODES,
    MODE_ALL,
    MODE_NEW,
    MODE_USED,
    ZIP_SHARDS,
)
from .listing_parser import parse_detail_html, parse_list_row

logger = logging.getLogger(__name__)


# Country pools that empirically pass Akamai's IP-reputation check for
# autotrader.com (2026-04 probe + in-page fetch verification):
#   jp:        ~50% session pass rate (proven)
#   pl/ph:     ~20-40% session pass rate (proven, kept for backward compat)
#   tw,kr:     predicted ~40-50% (clean Asia residential, low scraper density)
#   se,no,fi,dk: predicted ~50%+ (Nordic — premium ISPs, small proxy market)
#   ee,lt,lv:  predicted ~40-50% (Baltic — small market, premium ISPs)
#   cz,sk,hu,bg,si: predicted ~40-50% (Central Europe — low scraper burn)
#   at,ch,pt:  predicted ~40-50% (Western EU not in DE/FR/UK/NL burn cluster)
#   uy,cl,co,cr: predicted ~40-50% (Latam — small clean residential pools)
# us/ar:       0% (burned). de/fr/uk/nl: ~10-20% (burned proxy markets).
# Order doesn't matter — proxies are randomly shuffled before warmup.
AKAMAI_FRIENDLY_COUNTRIES = [
    "jp", "tw", "kr", "ph", "pl",
    "se", "no", "fi", "dk",
    "ee", "lt", "lv",
    "cz", "sk", "hu", "bg", "si",
    "at", "ch", "pt",
    "uy", "cl", "co", "cr",
]

# How many proxy attempts to make before giving up on a scrape run.
# Per-session success rate is roughly 1-in-5 (warmup passes but REST probe
# rejects ~70% of the time, plus some homepage-hard-blocks). 25 attempts
# ≈ 99.6% cumulative chance of landing one. Each attempt costs ~30-60 s.
MAX_PROXY_ATTEMPTS = 25


class AutoTraderComScraper(BaseScraper):
    """Classifieds from [AutoTrader.com](https://www.autotrader.com)."""

    SOURCE_NAME = "AutoTrader.com"
    SOURCE_DOMAIN = "autotrader.com"
    DATABASE = "classifieds"

    def __init__(
        self,
        min_price: int = 0,
        output_dir="output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter: VehicleFilter | None = None,
        mode: str = MODE_ALL,
    ):
        super().__init__(
            min_price=min_price,
            output_dir=output_dir,
            max_concurrent=max_concurrent,
            max_pages=max_pages,
            vehicle_filter=vehicle_filter,
        )
        if mode not in (MODE_USED, MODE_NEW, MODE_ALL):
            raise ValueError(f"AutoTrader.com mode must be 'used'/'new'/'all', got {mode!r}")
        self.mode = mode

    # --- Session handshake --------------------------------------------

    async def _open_working_session(self) -> AutoTraderSession | None:
        """Iterate the proxy pool until one proxy successfully warms up.

        Returns a live ``AutoTraderSession`` (browser open) on success,
        or ``None`` if every candidate failed.

        Only **proxy-level** failures (connection refused, 407 auth,
        DNS, timeout) flip ``is_bad`` in the DB. Akamai challenges are
        session-specific and don't imply the proxy itself is broken.
        """
        candidates = iter_available_proxies(
            limit=MAX_PROXY_ATTEMPTS, country_allow=AKAMAI_FRIENDLY_COUNTRIES,
        )
        if not candidates:
            logger.info(
                "No JP/PL/PH proxies available; falling back to full pool",
            )
            candidates = iter_available_proxies(limit=MAX_PROXY_ATTEMPTS)
        if not candidates:
            logger.error(
                "No non-bad proxies in Supabase 'proxies' table. Seed "
                "residential proxies first (see db/seeds/*.sql).",
            )
            return None

        logger.info(
            "Warming up against up to %s candidate proxies", len(candidates),
        )
        for i, proxy in enumerate(candidates, start=1):
            cc = "??"
            if "country-" in proxy.url:
                cc = proxy.url.split("country-", 1)[1][:2]
            redacted = f"{proxy.server} [{cc}]"
            logger.info("[%s/%s] Warming up via %s", i, len(candidates), redacted)
            session = AutoTraderSession(proxy=proxy)
            try:
                await session.warmup()
            except AkamaiChallengeError as e:
                logger.warning(
                    "Proxy %s Akamai-challenged (session issue, not proxy): %s",
                    redacted, e,
                )
                await session.close()
                continue
            except Exception as e:
                # Discriminate: only mark bad on errors that point at the
                # PROXY itself. Browser-runtime errors (Playwright not
                # installed, OOM, etc.) and our own code bugs would otherwise
                # nuke the proxy pool. Match by error-string substrings since
                # Playwright's exception types are split across submodules.
                msg = str(e)
                local_runtime_markers = (
                    "Executable doesn't exist",       # browser binary missing
                    "BrowserType.launch",             # general launch failure
                    "playwright._impl",               # internal launch errors
                    "asyncio.exceptions.CancelledError",
                    "Event loop is closed",
                )
                is_local_bug = any(m in msg for m in local_runtime_markers)
                if is_local_bug:
                    logger.error(
                        "Proxy %s LOCAL runtime error (NOT marking proxy bad): %s",
                        redacted, e,
                    )
                else:
                    logger.warning("Proxy %s proxy-level error: %s", redacted, e)
                    try:
                        mark_bad(proxy.url)
                    except Exception as mark_err:
                        logger.error("mark_bad DB error: %s", mark_err)
                await session.close()
                continue

            logger.info("Warmup OK via %s", redacted)
            return session

        logger.error(
            "Exhausted %s proxy attempts without a successful warmup.",
            len(candidates),
        )
        return None

    # --- Orchestration ------------------------------------------------

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("AutoTrader.com (%s) - Starting", self.mode.upper())
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info(
            "Max pages: %s (0=unlimited; site cap=%s records)",
            self.max_pages, HARD_RESULT_CAP,
        )
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        logger.info("=" * 60)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Resilient list scrape — opens its own session(s) and rotates
        # when one dies mid-run. Returns the (session, flat_rows) tuple
        # so the detail phase can continue with the same live session
        # rather than discarding it.
        session, flat_rows = await self._scrape_list_resilient()
        if not flat_rows:
            if session is not None:
                await session.close()
            return []

        logger.info("Collected %s listings - fetching details", len(flat_rows))
        # Detail fetches are the long pole (~10s per VDP, sequential per
        # session). With ``max_concurrent`` workers we open N independent
        # sessions in parallel — each gets its own proxy/browser and
        # scrapes a stride of the listings list. The first session
        # (already warmed during list scrape) handles chunk 0; the rest
        # warm fresh sessions in parallel. Default ``max_concurrent=4``
        # gives ~3-4x speedup without thrashing the proxy pool.
        try:
            details = await self._scrape_details_parallel(
                session, flat_rows, workers=max(1, self.max_concurrent),
            )
        finally:
            if session is not None:
                await session.close()

        # --- Build records -----------------------------------------------
        scraped_at = get_scraped_at()
        today_iso = datetime.now(tz=timezone.utc).date().isoformat()
        # Source tag mirrors other classifieds scrapers: bare domain for
        # the default / mixed feed, mode-suffixed only when the scrape is
        # explicitly scoped (used / new). Keeps ``mark_removed_classifieds``
        # per-mode without inventing a spurious ``/all`` suffix.
        source_tag = (
            "autotrader.com"
            if self.mode == MODE_ALL
            else f"autotrader.com/{self.mode}"
        )
        records = [
            record_builder.build_record(
                flat=flat,
                detail=details.get(flat["record_id"]),
                scraped_at=scraped_at,
                today_iso=today_iso,
                source=source_tag,
            )
            for flat in flat_rows
        ]
        logger.info("Built %s classifieds records", len(records))

        csv_path = self.output_dir / f"autotrader_com_{self.mode}_classifieds.csv"
        xlsx_path = self.output_dir / f"autotrader_com_{self.mode}_classifieds.xlsx"
        export_to_csv(records, csv_path, CLASSIFIEDS_COLUMNS)
        export_to_excel(records, xlsx_path, CLASSIFIEDS_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", xlsx_path.resolve())
        logger.info("=" * 60)
        logger.info("AutoTrader.com (%s) - Complete: %s records", self.mode.upper(), len(records))
        logger.info("=" * 60)
        return records

    # --- Internal steps -----------------------------------------------

    # Cap on how many fresh sessions we'll spin up across a single
    # scraper invocation. Each rotation costs ~30-60s of warmup; if we
    # need more than 20 the proxy pool is genuinely dead and we should
    # surface that rather than loop forever.
    _MAX_SESSION_ROTATIONS = 20

    async def _scrape_list_resilient(self) -> tuple[AutoTraderSession | None, list[dict]]:
        """Two-phase sharded list scrape with mid-run proxy rotation.

        AutoTrader's ``sortBy=derivedpriceDESC`` caps deep pagination at
        ~300-500 records per query, even in "national" mode (``searchRadius=0``).
        Each unique query signature (zip+radius+make combo) gets its own
        depth allowance, so we run two phases sequentially:

        **Phase 1 — Geo sharding** (~170 zip seeds at 100mi radius).
        Covers every US state. Surfaces dealer inventory by region.

        **Phase 2 — Make sharding** (~25 luxury makes, national radius).
        Each per-make query gets a fresh 500-cap depth. Catches dealers
        whose location no geo shard reached and high-volume make-specific
        inventory (Ferrari/Porsche specialists, etc.).

        Both phases share ``seen_urls`` so cross-phase dedup is automatic.
        Session resilience is per-shard: a session that fails triggers a
        rotation (re-warm with a fresh proxy) and the shard resumes from
        the same offset. Sessions persist across shards when healthy —
        we don't burn a warmup per shard.

        Returns ``(live_session, flat_rows)``. The session is kept alive
        for the subsequent detail-fetch phase so we don't burn a fresh
        warmup just to start the second stage.
        """
        flat_rows: list[dict] = []
        seen_urls: set[str] = set()
        session: AutoTraderSession | None = None
        rotations = 0
        total_reported_logged = False

        # Build the combined shard plan: ~170 geo + ~25 make = ~195 queries.
        # Each shard descriptor is (label, query-kwargs-for-fetch_list_page).
        geo_shards = [
            (
                f"{city},{state} ({zip_code})",
                {
                    "zip_code": zip_code, "city": city, "state": state,
                    "radius": DEFAULT_SHARD_RADIUS_MI, "make_code": None,
                },
            )
            for zip_code, city, state in ZIP_SHARDS
        ]
        make_shards = [
            (
                f"{name} (national)",
                {
                    "zip_code": None, "city": None, "state": None,
                    "radius": 0, "make_code": code,
                },
            )
            for name, code in LUXURY_MAKE_CODES
        ]
        all_shards = geo_shards + make_shards

        for shard_idx, (shard_label, query_kwargs) in enumerate(all_shards, start=1):
            phase_tag = "geo" if shard_idx <= len(geo_shards) else "make"
            logger.info(
                "=== Shard %s/%s [%s]: %s ===",
                shard_idx, len(all_shards), phase_tag, shard_label,
            )

            first_record = 0
            page = 0
            consecutive_failures = 0
            shard_added = 0
            shard_consecutive_all_dupes = 0
            # Per-shard caps. AutoTrader stops serving past ~500 even
            # within a metro; capping at 600 gives slack for the case
            # where a few duplicate pages get retried via rotation.
            SHARD_MAX_OFFSET = 600

            while True:
                if rotations >= self._MAX_SESSION_ROTATIONS:
                    logger.error(
                        "Exhausted %s session rotations; stopping all shards "
                        "with %s rows collected so far.",
                        self._MAX_SESSION_ROTATIONS, len(flat_rows),
                    )
                    return session, flat_rows

                if session is None:
                    session = await self._open_working_session()
                    if session is None:
                        logger.error(
                            "No working session available; stopping list scrape "
                            "at shard %s/%s, %s rows collected.",
                            shard_idx, len(ZIP_SHARDS), len(flat_rows),
                        )
                        return session, flat_rows
                    rotations += 1
                    logger.info(
                        "Active session #%s — shard %s resuming from offset %s",
                        rotations, shard_label, first_record,
                    )

                page += 1
                try:
                    payload = await session.fetch_list_page(
                        mode=self.mode,
                        first_record=first_record,
                        **query_kwargs,
                    )
                except AkamaiChallengeError as e:
                    logger.warning(
                        "Shard %s page %s (offset %s) Akamai-challenged: %s - "
                        "rotating session", shard_label, page, first_record, e,
                    )
                    await session.close()
                    session = None
                    page -= 1
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.error(
                            "3 consecutive challenges on shard %s offset %s; "
                            "moving to next shard",
                            shard_label, first_record,
                        )
                        break
                    continue
                except Exception as e:
                    logger.warning(
                        "Shard %s page %s (offset %s) failed: %s - rotating",
                        shard_label, page, first_record, e,
                    )
                    await session.close()
                    session = None
                    page -= 1
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        logger.error(
                            "3 consecutive failures on shard %s offset %s; "
                            "moving to next shard",
                            shard_label, first_record,
                        )
                        break
                    continue

                consecutive_failures = 0  # successful fetch

                listings = payload.get("listings") or []
                if not total_reported_logged:
                    total_reported = (
                        payload.get("totalResultCount")
                        or payload.get("totalCount")
                        or payload.get("resultCount")
                    )
                    logger.info(
                        "AutoTrader.com reports %s total listings for first "
                        "shard query (site cap = %s)",
                        total_reported, HARD_RESULT_CAP,
                    )
                    total_reported_logged = True

                if not listings:
                    logger.info(
                        "Shard %s exhausted (empty page at offset %s)",
                        shard_label, first_record,
                    )
                    break

                page_added = 0
                page_dupes = 0
                stop_low_price = False
                for row in listings:
                    flat = parse_list_row(row)
                    if not flat:
                        continue
                    price = flat.get("asking_price")
                    if price is not None and price < self.min_price:
                        stop_low_price = True
                        break
                    if self.vehicle_filter and flat.get("title"):
                        if not self.vehicle_filter.match(flat["title"], ""):
                            continue
                    url = flat.get("listing_url")
                    if url and url in seen_urls:
                        page_dupes += 1
                        continue
                    if url:
                        seen_urls.add(url)
                    flat_rows.append(flat)
                    page_added += 1
                    shard_added += 1

                logger.info(
                    "Shard %s page %s (offset %s): %s listings -> %s new, %s dupes "
                    "(shard %s, grand total %s)",
                    shard_label, page, first_record, len(listings),
                    page_added, page_dupes, shard_added, len(flat_rows),
                )

                # Stop conditions for THIS shard (not the whole scrape):
                if stop_low_price:
                    logger.info(
                        "Shard %s hit min-price floor ($%s) - moving to next shard",
                        shard_label, f"{self.min_price:,}",
                    )
                    break
                # If a whole page returned no new URLs, we're scraping pure
                # overlap. After 2 such consecutive pages this shard is mined out.
                if page_added == 0 and len(listings) > 0:
                    shard_consecutive_all_dupes += 1
                    if shard_consecutive_all_dupes >= 2:
                        logger.info(
                            "Shard %s page %s was 100%% dupes (2 in a row) — "
                            "moving to next shard",
                            shard_label, page,
                        )
                        break
                else:
                    shard_consecutive_all_dupes = 0

                if self.max_pages > 0 and page >= self.max_pages:
                    logger.info(
                        "Shard %s hit --max-pages cap (%s) - moving to next shard",
                        shard_label, self.max_pages,
                    )
                    break
                if first_record + DEFAULT_PAGE_SIZE >= SHARD_MAX_OFFSET:
                    logger.info(
                        "Shard %s hit per-shard offset cap (%s) - moving to next "
                        "shard", shard_label, SHARD_MAX_OFFSET,
                    )
                    break

                first_record += DEFAULT_PAGE_SIZE
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.2, 0.6))

        return session, flat_rows

    async def _scrape_details_parallel(
        self,
        seed_session: AutoTraderSession | None,
        flat_rows: list[dict],
        *,
        workers: int,
    ) -> dict[str, dict]:
        """Fan the detail scrape across ``workers`` parallel sessions.

        Strategy:
          * Slice ``flat_rows`` round-robin across ``workers`` chunks so
            every worker covers a mix of price tiers (avoids one worker
            getting stuck on a slow cluster).
          * Worker 0 reuses the already-warmed ``seed_session`` so we
            don't waste the ~2 min warmup that just succeeded.
          * Workers 1..N-1 each open a fresh session (own proxy + browser).
          * Each worker runs the existing ``_scrape_details_resilient``
            loop, which rotates its own session on Akamai failures.
          * Results merge into one dict.

        Trade-off: ``workers`` browsers = ``workers`` × ~500MB RAM and
        proportionally more proxy attempts. Default 4 is a good balance
        for typical hardware and Akamai tolerance.
        """
        if not flat_rows:
            return {}
        workers = max(1, min(workers, len(flat_rows)))

        chunks: list[list[dict]] = [[] for _ in range(workers)]
        for i, row in enumerate(flat_rows):
            chunks[i % workers].append(row)

        logger.info(
            "Detail scrape: %s workers, chunk sizes=%s",
            workers, [len(c) for c in chunks],
        )

        async def _worker(idx: int, chunk: list[dict]) -> dict[str, dict]:
            # Worker 0 reuses the seed session; others open their own.
            sess = seed_session if idx == 0 else None
            logger.info(
                "Detail worker %s starting (%s rows, %s session)",
                idx, len(chunk), "reused" if sess else "fresh",
            )
            result = await self._scrape_details_resilient(sess, chunk)
            logger.info(
                "Detail worker %s done (%s/%s rows)", idx, len(result), len(chunk),
            )
            return result

        results = await asyncio.gather(
            *(_worker(i, c) for i, c in enumerate(chunks)),
            return_exceptions=True,
        )
        merged: dict[str, dict] = {}
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                logger.error("Detail worker %s crashed: %s", i, r)
                continue
            merged.update(r)
        return merged

    async def _scrape_details_resilient(
        self,
        session: AutoTraderSession | None,
        flat_rows: list[dict],
    ) -> dict[str, dict]:
        """VDP fetcher with mid-run proxy rotation.

        Same resilience pattern as ``_scrape_list_resilient``: when the
        session dies (Akamai challenge, network error, broken page state),
        we close it, open a fresh one, and continue from the next index.
        Per-row failures are tolerated (left in the dict as ``{}``) and
        skipped after one retry — losing a few VDP descriptions out of
        thousands is better than aborting the whole detail phase.
        """
        details: dict[str, dict] = {}
        total = len(flat_rows)
        rotations = 0
        i = 0
        # Track whether the CURRENTLY-held session was opened by us (in
        # which case we own its lifecycle and must close it on exit) or
        # was handed in by the caller (caller still owns it). Only the
        # seed session — the one originally passed in — belongs to the
        # caller; every rotation thereafter creates a session we own.
        owns_session = False
        try:
            while i < total:
                if session is None:
                    if rotations >= self._MAX_SESSION_ROTATIONS:
                        logger.error(
                            "Exhausted %s session rotations during detail scrape; "
                            "stopping at %s/%s.",
                            self._MAX_SESSION_ROTATIONS, i, total,
                        )
                        break
                    session = await self._open_working_session()
                    if session is None:
                        logger.error(
                            "No working session available; stopping detail scrape "
                            "at %s/%s.", i, total,
                        )
                        break
                    owns_session = True
                    rotations += 1
                    logger.info(
                        "Active detail session #%s — resuming at index %s",
                        rotations, i,
                    )

                flat = flat_rows[i]
                key = flat["record_id"]
                parsed: dict = {}
                try:
                    raw = await session.fetch_detail_html(url=flat["listing_url"])
                    parsed = parse_detail_html(raw) if raw else {}
                    if not (parsed.get("description") or "").strip():
                        rendered = await session.fetch_detail_rendered(
                            url=flat["listing_url"],
                        )
                        if rendered:
                            re_parsed = parse_detail_html(rendered)
                            if (re_parsed.get("description") or "").strip():
                                parsed = re_parsed
                except AkamaiChallengeError as e:
                    logger.warning(
                        "Detail %s Akamai-challenged: %s - rotating session",
                        key, e,
                    )
                    await session.close()
                    session = None
                    owns_session = False
                    continue  # retry same index with new session
                except Exception as e:
                    msg = str(e)
                    # Per-row transient errors stay per-row; session-level errors
                    # (NetworkError, thread-binding, page closed) rotate the
                    # session before continuing. Heuristic on message string.
                    session_level = any(m in msg for m in (
                        "NetworkError", "Cannot switch", "Target closed",
                        "Browser closed", "Connection closed",
                    ))
                    if session_level:
                        logger.warning(
                            "Detail %s session-level error: %s - rotating session",
                            key, e,
                        )
                        await session.close()
                        session = None
                        owns_session = False
                        continue  # retry same index
                    logger.debug("detail %s failed: %s", key, e)

                details[key] = parsed
                i += 1
                if i % 25 == 0 or i == total:
                    logger.info("Detail fetch %s/%s", i, total)
                await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.6 + random.uniform(0.3, 0.7))
        finally:
            # Close any session WE created (don't touch the seed — caller owns it).
            if owns_session and session is not None:
                try:
                    await session.close()
                except Exception as e:
                    logger.debug("Closing worker-owned session failed: %s", e)
        return details
