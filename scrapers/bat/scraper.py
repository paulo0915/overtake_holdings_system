"""BaT scraper — uses shared pipeline for normalization, filtering, export."""

import asyncio
import html as html_lib
import json
import logging
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from shared.config import MAX_CONCURRENT_REQUESTS, MIN_PRICE_USD, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT
from shared.fx_converter import FXConverter
from shared.normalizer import (
    country_code_to_name,
    format_selling_date,
    get_scraped_at,
    normalize_engine_spec,
    normalize_mileage,
    normalize_transmission,
    parse_make_model_from_title,
    parse_year_from_title,
    unescape,
)
from shared.schemas import AUCTION_COLUMNS
from shared.vehicle_filter import VehicleFilter

from .api_client import BaTAPIClient
from .config import get_page_headers
from .listing_parser import ListingParser

logger = logging.getLogger(__name__)


def _parse_sold_text(sold_text: str | None) -> tuple[str, int | None, int | None, str | None]:
    """Parse BaT sold_text. Returns (status, selling_price, bid_price, date_str)."""
    import re

    if not sold_text:
        return ("Withdrawn", None, None, None)

    sold_text = sold_text.strip()

    if sold_text.lower().startswith("sold"):
        status = "Sold"
    elif sold_text.lower().startswith("bid to"):
        status = "Unsold"
    elif "withdrawn" in sold_text.lower():
        status = "Withdrawn"
    else:
        status = "Unsold"

    price_match = re.search(r'(?:[\$€£]|CHF)\s*([\d,]+)', sold_text)
    price = int(price_match.group(1).replace(",", "")) if price_match else None

    date_match = re.search(r'on\s+(\d{1,2})/(\d{1,2})/(\d{4})', sold_text)
    date_str = None
    if date_match:
        month, day, year = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
        date_str = f"{year:04d}-{month:02d}-{day:02d}"

    selling_price = price if status == "Sold" else None
    bid_price = price if status == "Unsold" else None
    return (status, selling_price, bid_price, date_str)


class BaTScraper:
    """Orchestrates BaT scraping with shared pipeline."""

    def __init__(
        self,
        min_price: int = MIN_PRICE_USD,
        output_dir: str | Path = "output",
        max_concurrent: int = MAX_CONCURRENT_REQUESTS,
        max_pages: int = 0,
        vehicle_filter: VehicleFilter | None = None,
    ):
        self.min_price = min_price
        self.output_dir = Path(output_dir)
        self.max_concurrent = max_concurrent
        self.max_pages = max_pages
        self.parser = ListingParser()
        self.vehicle_filter = vehicle_filter
        self._checkpoint_path = self.output_dir / ".checkpoint.json"

    async def run(self) -> list[dict]:
        """Run the full BaT scraping pipeline."""
        logger.info("=" * 60)
        logger.info("BaT Scraper - Starting")
        logger.info(f"Min price: ${self.min_price:,}")
        logger.info(f"Concurrency: {self.max_concurrent}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(http2=True, follow_redirects=True) as client:
            # Session warmup
            await self._warmup_session(client)

            # Step 1: API pagination (or resume from checkpoint)
            checkpoint = self._load_checkpoint()
            if checkpoint and checkpoint.get("api_listings"):
                raw_listings = checkpoint["api_listings"]
                logger.info(f"Resumed {len(raw_listings)} listings from checkpoint")
            else:
                api_client = BaTAPIClient(client, self.min_price)
                raw_listings = await api_client.fetch_all_above_threshold(self.max_pages)
                logger.info(f"API returned {len(raw_listings)} listings >= ${self.min_price:,}")

            if not raw_listings:
                logger.warning("No listings found. Exiting.")
                return []

            # Step 2: Fetch listing pages (with resume)
            already_parsed = checkpoint.get("parsed_details", {}) if checkpoint else {}
            if already_parsed:
                logger.info(f"Checkpoint has {len(already_parsed)} already-parsed listings")

            semaphore = asyncio.Semaphore(self.max_concurrent)
            details = await self._fetch_listing_details(
                client, raw_listings, semaphore, already_parsed
            )

            # Step 3: Build + normalize + filter records
            fx_converter = FXConverter(client)
            records = await self._build_records(raw_listings, details, fx_converter)
            logger.info(f"Built {len(records)} final records")

            # Step 4: Export CSV/Excel
            from shared.export import export_to_csv, export_to_excel
            csv_path = self.output_dir / "bat_auction_results.csv"
            excel_path = self.output_dir / "bat_auction_results.xlsx"
            export_to_csv(records, csv_path, AUCTION_COLUMNS)
            export_to_excel(records, excel_path, AUCTION_COLUMNS)

            self._delete_checkpoint()

            logger.info("=" * 60)
            logger.info("BaT Scraper - Complete")
            logger.info(f"Total records: {len(records)}")
            logger.info(f"CSV: {csv_path}")
            logger.info(f"Excel: {excel_path}")
            logger.info("=" * 60)

            return records

    # ── Session warmup ──────────────────────────────────────

    async def _warmup_session(self, client: httpx.AsyncClient) -> None:
        logger.info("Warming up session...")
        try:
            resp = await client.get(
                "https://bringatrailer.com/",
                headers=get_page_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            logger.info(f"Session warmup done (status={resp.status_code}, cookies={len(client.cookies)})")
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.warning(f"Session warmup failed: {e}")

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load checkpoint: {e}")
            return None

    def _save_checkpoint(self, api_listings: list[dict], parsed_details: dict[str, dict]) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"api_listings": api_listings, "parsed_details": parsed_details},
                       ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")

    # ── Listing page fetching ───────────────────────────────

    async def _fetch_single_listing(self, client, url, semaphore) -> str | None:
        async with semaphore:
            for attempt in range(3):
                try:
                    resp = await client.get(url, headers=get_page_headers(), timeout=REQUEST_TIMEOUT)
                    if resp.status_code in (429, 403):
                        wait = 15 * (attempt + 1) + random.uniform(0, 5)
                        logger.warning(f"{resp.status_code} on {url}, waiting {wait:.0f}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    html = resp.text
                    if any(s in html[:5000] for s in ("cf-browser-verification", "cf_chl_opt", "Just a moment...")):
                        await asyncio.sleep(15 * (attempt + 1) + random.uniform(0, 5))
                        continue
                    await asyncio.sleep(REQUEST_DELAY_SECONDS + random.uniform(0.5, 2.0))
                    return html
                except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                    if attempt == 2:
                        logger.warning(f"Failed {url} after 3 attempts: {e}")
                        return None
                    await asyncio.sleep(3 * (attempt + 1) + random.uniform(0, 2))
        return None

    async def _fetch_listing_details(self, client, listings, semaphore, already_parsed):
        results = dict(already_parsed)
        to_fetch = [i for i in listings if i.get("url", "") not in results]

        if not to_fetch:
            logger.info("All listings in checkpoint. Skipping fetch.")
            return results

        logger.info(f"Fetching {len(to_fetch)} pages ({len(already_parsed)} cached)...")
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=self.max_concurrent)

        async def fetch_and_parse(item):
            url = item.get("url", "")
            try:
                html = await self._fetch_single_listing(client, url, semaphore)
                if html:
                    parsed = await loop.run_in_executor(executor, self.parser.parse, html, url)
                    return (url, parsed)
            except Exception as e:
                logger.warning(f"Error on {url}: {e}")
            return (url, {})

        tasks = [fetch_and_parse(i) for i in to_fetch]
        done = 0
        for coro in asyncio.as_completed(tasks):
            url, parsed = await coro
            results[url] = parsed
            done += 1
            if done % 50 == 0:
                logger.info(f"Fetched {done}/{len(to_fetch)} (saving checkpoint)")
                self._save_checkpoint(listings, results)
            elif done == len(to_fetch):
                logger.info(f"Fetched {done}/{len(to_fetch)}")

        executor.shutdown(wait=True)
        self._save_checkpoint(listings, results)
        return results

    # ── Record building ─────────────────────────────────────

    async def _build_records(self, listings, details, fx_converter):
        records = []
        scraped_at = get_scraped_at()
        seen_urls: set[str] = set()
        skipped = {"parts": 0, "invalid": 0, "filter": 0, "dup": 0}

        for item in listings:
            url = item.get("url", "")
            if url in seen_urls:
                skipped["dup"] += 1
                continue
            seen_urls.add(url)

            title = html_lib.unescape(item.get("title", ""))
            detail = details.get(url, {})

            if not detail.get("is_valid_listing", True):
                skipped["invalid"] += 1
                continue
            if detail.get("is_parts", False):
                skipped["parts"] += 1
                continue

            # Make & Model
            make = detail.get("make") or None
            model = detail.get("model") or None
            if not make:
                make, model = parse_make_model_from_title(title)
                make = make or None
                model = model or None

            # Vehicle filter
            if self.vehicle_filter and make:
                match = self.vehicle_filter.match(title, "")
                if not match:
                    skipped["filter"] += 1
                    continue

            # Sold text
            sold_text = item.get("sold_text", "")
            sold_status, selling_price, bid_price, selling_date_str = _parse_sold_text(sold_text)
            selling_date = format_selling_date(item.get("timestamp_end"), selling_date_str)

            year = detail.get("year") or parse_year_from_title(title)
            mileage = normalize_mileage(detail.get("mileage_raw"), detail.get("mileage_unit"))
            transmission = normalize_transmission(detail.get("transmission_raw"))
            engine = normalize_engine_spec(detail.get("engine_spec"))

            # FX
            currency = item.get("currency", "USD")
            price_usd, fx_rate = (None, None)
            bid_price_usd = None

            if selling_price is not None:
                price_usd, fx_rate = await fx_converter.convert_to_usd(selling_price, currency, selling_date)
            if bid_price is not None:
                bid_price_usd, bid_fx = await fx_converter.convert_to_usd(bid_price, currency, selling_date)
                if fx_rate is None:
                    fx_rate = bid_fx

            country = country_code_to_name(item.get("country_code"))
            image_url = item.get("thumbnail_url")
            if image_url and "?resize=" in image_url:
                image_url = image_url.split("?resize=")[0]

            bat_id = item.get("id")

            records.append({
                "Record_ID": str(bat_id) if bat_id else url,
                "Title": title,
                "Year": year,
                "Make": make,
                "Model": model,
                "Authenticity": detail.get("authenticity", "Original"),
                "VIN": detail.get("vin"),
                "Mileage_mi": mileage,
                "Ext_color": unescape(detail.get("ext_color")),
                "Int_color": unescape(detail.get("int_color")),
                "Engine_spec": engine,
                "Transmission": transmission,
                "Lot_number": detail.get("lot_number"),
                "Estimate_low": None,
                "Estimate_high": None,
                "Estimate_currency": None,
                "Sold_status": sold_status,
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": price_usd,
                "Bid_price": bid_price,
                "Bid_price_USD": bid_price_usd,
                "Bid_count": detail.get("bid_count"),
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Bring a Trailer",
                "Country": country,
                "Image_url": image_url,
                "Listing_url": url,
                "Source": "bringatrailer.com",
                "Listing_description": detail.get("listing_description"),
                "Scraped_at": scraped_at,
            })

        for k, v in skipped.items():
            if v:
                logger.info(f"Skipped {v} ({k})")
        return records
