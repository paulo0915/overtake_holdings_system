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
from .config import DEFAULT_PAGE_SIZE, HARD_RESULT_CAP, MODE_ALL, MODE_NEW, MODE_USED
from .listing_parser import parse_detail_html, parse_list_row

logger = logging.getLogger(__name__)


# Country pools that empirically pass Akamai's IP-reputation check for
# autotrader.com (2026-04 probe + in-page fetch verification):
#   jp: ~50% session pass rate
#   pl: ~20-30% session pass rate (sometimes proxy-level timeouts)
#   ph: ~30-40% session pass rate
# us/ar: 0% (burned). We allow-list the good countries and accept the
# per-session noise by retrying up to N different proxies.
AKAMAI_FRIENDLY_COUNTRIES = ["jp", "pl", "ph"]

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
                # Proxy-level: timeout, auth, DNS, connection refused.
                logger.warning("Proxy %s proxy-level error: %s", redacted, e)
                await session.close()
                try:
                    mark_bad(proxy.url)
                except Exception as mark_err:
                    logger.error("mark_bad DB error: %s", mark_err)
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

        session = await self._open_working_session()
        if session is None:
            return []

        try:
            flat_rows = await self._scrape_list(session)
            logger.info("Collected %s listings - fetching details", len(flat_rows))
            details = await self._scrape_details(session, flat_rows)
        finally:
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

    async def _scrape_list(self, session: AutoTraderSession) -> list[dict]:
        """Paginate ``/rest/lsc/listing`` via the browser until we run
        out, hit min-price, or exceed ``--max-pages``.
        """
        flat_rows: list[dict] = []
        first_record = 0
        page = 0
        total_reported: int | None = None
        while True:
            page += 1
            try:
                payload = await session.fetch_list_page(
                    mode=self.mode, first_record=first_record,
                )
            except AkamaiChallengeError as e:
                # Mid-run challenge — cookies went stale. Bail on the
                # rest of this run rather than silently producing partial
                # data. Next invocation will re-warm and resume.
                logger.error("List page %s Akamai-challenged: %s", page, e)
                break
            except Exception as e:
                logger.error("List page %s failed: %s", page, e)
                break

            listings = payload.get("listings") or []
            if total_reported is None:
                total_reported = (
                    payload.get("totalResultCount")
                    or payload.get("totalCount")
                    or payload.get("resultCount")
                )
                logger.info(
                    "AutoTrader.com reports %s total listings (site cap = %s)",
                    total_reported, HARD_RESULT_CAP,
                )

            if not listings:
                break

            page_added = 0
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
                flat_rows.append(flat)
                page_added += 1

            logger.info(
                "Page %s (offset %s): %s listings -> %s kept (total %s)",
                page, first_record, len(listings), page_added, len(flat_rows),
            )

            if stop_low_price:
                logger.info("Hit min-price floor ($%s) - stopping", f"{self.min_price:,}")
                break
            if self.max_pages > 0 and page >= self.max_pages:
                logger.info("Max pages (%s) reached - stopping", self.max_pages)
                break
            if first_record + DEFAULT_PAGE_SIZE >= HARD_RESULT_CAP:
                logger.info("Reached 10k result cap - stopping")
                break

            first_record += DEFAULT_PAGE_SIZE
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.5 + random.uniform(0.2, 0.6))

        return flat_rows

    async def _scrape_details(
        self, session: AutoTraderSession, flat_rows: list[dict],
    ) -> dict[str, dict]:
        """Fetch VDP HTML for each listing and extract dealer description.

        Sequential (concurrency=1) — all requests share the same browser
        page, so we can't parallelise without additional context tabs.
        Akamai is also more tolerant of paced sequential traffic than
        concurrent bursts.
        """
        details: dict[str, dict] = {}
        total = len(flat_rows)
        for i, flat in enumerate(flat_rows, 1):
            key = flat["record_id"]
            parsed: dict = {}
            try:
                raw = await session.fetch_detail_html(url=flat["listing_url"])
                parsed = parse_detail_html(raw) if raw else {}
                # If fast path didn't produce a real description, render
                # the VDP so client-side-hydrated fields (some dealers
                # push notes via XHR after mount) come into scope.
                if not (parsed.get("description") or "").strip():
                    rendered = await session.fetch_detail_rendered(
                        url=flat["listing_url"],
                    )
                    if rendered:
                        re_parsed = parse_detail_html(rendered)
                        if (re_parsed.get("description") or "").strip():
                            parsed = re_parsed
            except AkamaiChallengeError:
                logger.warning("detail %s Akamai-challenged, skipping", key)
            except Exception as e:
                logger.debug("detail %s failed: %s", key, e)
            details[key] = parsed
            if i % 25 == 0 or i == total:
                logger.info("Detail fetch %s/%s", i, total)
            await asyncio.sleep(REQUEST_DELAY_SECONDS * 0.6 + random.uniform(0.3, 0.7))
        return details
