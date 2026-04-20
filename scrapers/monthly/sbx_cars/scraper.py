"""SBX Cars — auction results from /results + per-listing pages."""

from __future__ import annotations

import asyncio
import json
import logging
import random

import httpx
from curl_cffi.requests import AsyncSession

from shared.base_scraper import BaseScraper
from shared.config import REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.export import export_to_csv, export_to_excel
from shared.fx_converter import FXConverter
from shared.normalizer import get_scraped_at
from shared.schemas import AUCTION_COLUMNS

from . import api_client, record_builder
from .config import RESULTS_REFERER
from .listing_parser import (
    merge_card_and_detail,
    parse_detail_html,
    parse_results_cards,
    resolve_price_from_detail,
)

logger = logging.getLogger(__name__)


class SbxCarsScraper(BaseScraper):
    """Ended auctions from [SBX Cars](https://sbxcars.com/results)."""

    SOURCE_NAME = "SBX Cars"
    SOURCE_DOMAIN = "sbxcars.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = self.output_dir / ".sbx_cars_checkpoint.json"

    async def run(self) -> list[dict]:
        logger.info("=" * 60)
        logger.info("SBX Cars — Starting")
        logger.info("Min price (USD): $%s", f"{self.min_price:,}")
        logger.info("Concurrency: %s", self.max_concurrent)
        logger.info("Vehicle filter: %s", "active" if self.vehicle_filter else "disabled")
        if self.max_pages > 0:
            logger.info("Max listings (test mode): %s", self.max_pages)
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = self._load_checkpoint()
        cached_details = (checkpoint or {}).get("details", {}) if checkpoint else {}

        records: list[dict] = []

        async with AsyncSession(impersonate="chrome124") as session:
            await session.get(RESULTS_REFERER, timeout=float(REQUEST_TIMEOUT))

            html = await api_client.fetch_results_html(session)
            if not html:
                logger.error("Failed to fetch /results")
                return []

            cards = parse_results_cards(html)
            if self.max_pages > 0:
                cards = cards[: self.max_pages]
            logger.info("Parsed %s unique listing cards from /results", len(cards))

            details: dict[str, dict] = {k: v for k, v in cached_details.items() if isinstance(v, dict)}

            no_card_price = [c for c in cards if c.get("card_amount") is None]
            await self._fetch_details_batch(session, no_card_price, details)

            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as fx_client:
                fx = FXConverter(fx_client)
                filtered = await self._filter_cards_by_min_price(cards, details, fx)

                await self._fetch_details_batch(session, filtered, details)

                merged = [merge_card_and_detail(c, details.get(c["listing_id"], {})) for c in filtered]

                scraped_at = get_scraped_at()
                records = await record_builder.build_records(
                    merged, fx, self.vehicle_filter, scraped_at,
                )

            logger.info("Built %s records", len(records))

        csv_path = self.output_dir / "sbx_cars_auction_results.csv"
        excel_path = self.output_dir / "sbx_cars_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)
        logger.info("Output CSV:   %s", csv_path.resolve())
        logger.info("Output Excel: %s", excel_path.resolve())

        self._delete_checkpoint()
        logger.info("=" * 60)
        logger.info("SBX Cars — Complete: %s records", len(records))
        logger.info("=" * 60)
        return records

    async def _usd_for_min_filter(
        self,
        card: dict,
        detail: dict,
        fx: FXConverter,
    ) -> float | None:
        if card.get("card_amount") is not None and card.get("card_currency"):
            amt, cur = card["card_amount"], card["card_currency"]
            date_str = detail.get("selling_date")
            usd, _ = await fx.convert_to_usd(amt, cur, date_str)
            return usd
        amt, cur = resolve_price_from_detail(detail)
        if amt is None:
            return None
        date_str = detail.get("selling_date")
        usd, _ = await fx.convert_to_usd(amt, cur, date_str)
        return usd

    async def _filter_cards_by_min_price(
        self,
        cards: list[dict],
        details: dict[str, dict],
        fx: FXConverter,
    ) -> list[dict]:
        if self.min_price <= 0:
            return list(cards)

        out: list[dict] = []
        for c in cards:
            lid = c["listing_id"]
            d = details.get(lid, {})
            if c.get("card_amount") is None:
                status = (d.get("auction_status") or "").strip()
                if status not in ("Sold", "SoldPrivately"):
                    continue
            usd = await self._usd_for_min_filter(c, d, fx)
            if usd is None:
                continue
            if usd >= self.min_price:
                out.append(c)

        logger.info(
            "Min-price filter: %s / %s cards kept (>= $%s)",
            len(out),
            len(cards),
            f"{self.min_price:,}",
        )
        return out

    async def _fetch_details_batch(
        self,
        session: AsyncSession,
        cards: list[dict],
        details: dict[str, dict],
    ) -> None:
        pending = [c for c in cards if c.get("listing_id") and c["listing_id"] not in details]
        if not pending:
            return

        sem = asyncio.Semaphore(self.max_concurrent)
        logger.info("Fetching %s listing pages …", len(pending))

        async def one(card: dict) -> tuple[str, dict]:
            lid = card["listing_id"]
            path = card.get("listing_path") or f"/listing/{lid}/{card.get('slug', '')}"
            async with sem:
                try:
                    raw = await api_client.fetch_listing_html(session, path)
                    d = parse_detail_html(raw)
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.05, 0.35))
                    return lid, d
                except Exception as e:
                    logger.warning("Listing fetch failed %s: %s", path, e)
                    return lid, {}

        tasks = [asyncio.create_task(one(c)) for c in pending]
        done = 0
        for coro in asyncio.as_completed(tasks):
            lid, d = await coro
            details[lid] = d
            done += 1
            if done % 10 == 0 or done == len(tasks):
                self._save_checkpoint(details)
                logger.info("Listing detail progress %s/%s", done, len(tasks))

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, details: dict[str, dict]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
