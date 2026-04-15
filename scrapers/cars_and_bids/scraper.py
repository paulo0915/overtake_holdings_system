"""Cars & Bids scraper — uses Playwright to bypass Cloudflare, intercepts API responses."""

import html as html_lib
import json
import logging
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

from shared.base_scraper import BaseScraper
from shared.config import MIN_PRICE_USD, MAX_CONCURRENT_REQUESTS, REQUEST_DELAY_SECONDS
from shared.normalizer import (
    country_code_to_name,
    get_scraped_at,
    normalize_engine_spec,
    normalize_mileage,
    unescape,
)
from shared.schemas import AUCTION_COLUMNS
from shared.vehicle_filter import VehicleFilter

from .config import (
    CAB_BASE_URL,
    CAB_RESULTS_PER_PAGE,
    STATUS_MAP,
    TRANSMISSION_MAP,
)

logger = logging.getLogger(__name__)


class CarsAndBidsScraper(BaseScraper):
    """Cars & Bids scraper using Playwright for Cloudflare bypass."""

    SOURCE_NAME = "Cars & Bids"
    SOURCE_DOMAIN = "carsandbids.com"
    DATABASE = "auctions"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._checkpoint_path = Path(self.output_dir) / ".cab_checkpoint.json"

    async def run(self) -> list[dict]:
        """Async interface — delegates to run_sync."""
        return self.run_sync()

    def run_sync(self) -> list[dict]:
        """Run the Cars & Bids scraper using sync Playwright."""
        logger.info("=" * 60)
        logger.info("Cars & Bids Scraper - Starting")
        logger.info(f"Min price: ${self.min_price:,}")
        logger.info(f"Vehicle filter: {'active' if self.vehicle_filter else 'disabled'}")
        if self.max_pages > 0:
            logger.info(f"Max pages: {self.max_pages} (test mode)")
        logger.info("=" * 60)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Load checkpoint
        checkpoint = self._load_checkpoint()
        cached_listings = checkpoint.get("listings", []) if checkpoint else []
        cached_details = checkpoint.get("details", {}) if checkpoint else {}

        if cached_listings:
            logger.info(f"Resumed {len(cached_listings)} listings from checkpoint")
            all_listings = cached_listings
        else:
            all_listings = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            # Stealth: hide webdriver flag
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            try:
                page = context.new_page()

                # Step 1: Fetch auction list (if not cached)
                if not cached_listings:
                    all_listings = self._fetch_auction_list(page)
                    logger.info(f"Fetched {len(all_listings)} listings >= ${self.min_price:,}")

                    if not all_listings:
                        logger.warning("No listings found.")
                        return []

                # Step 2: Fetch detail pages for each listing
                records = self._fetch_details_and_build_records(
                    page, context, all_listings, cached_details,
                )
            finally:
                browser.close()

        # Step 3: Export
        from shared.export import export_to_csv, export_to_excel
        csv_path = self.output_dir / "cab_auction_results.csv"
        excel_path = self.output_dir / "cab_auction_results.xlsx"
        export_to_csv(records, csv_path, AUCTION_COLUMNS)
        export_to_excel(records, excel_path, AUCTION_COLUMNS)

        self._delete_checkpoint()

        logger.info("=" * 60)
        logger.info(f"Cars & Bids Scraper - Complete: {len(records)} records")
        logger.info(f"CSV: {csv_path}")
        logger.info(f"Excel: {excel_path}")
        logger.info("=" * 60)

        return records

    # ── Auction list fetching ───────────────────────────────

    def _fetch_auction_list(self, page: Page) -> list[dict]:
        """
        Navigate past-auctions pages sorted by highest price (sort=9).

        Stops when price drops below min_price or max_pages is reached.
        """
        all_listings = []
        page_num = 0

        # Capture API responses
        captured = []

        def on_response(response):
            if "/v2/autos/auctions" in response.url and "status=closed" in response.url:
                try:
                    captured.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        # sort=highest_price in URL -> API translates to sort=10
        logger.info("Loading past auctions (sorted by highest price)...")
        try:
            page.goto(
                f"{CAB_BASE_URL}/past-auctions/?sort=highest_price",
                timeout=90000,
                wait_until="domcontentloaded",
            )
        except Exception:
            pass
        page.wait_for_timeout(12000)

        if not captured:
            logger.error("No API data captured. Cloudflare may have blocked.")
            return []

        # Process first page
        data = captured[-1]
        total = data.get("total", 0)
        auctions = data.get("auctions", [])
        below = self._collect_above_threshold(auctions, all_listings)
        page_num = 1

        logger.info(f"Page 1: {len(auctions)} auctions (total: {total}), kept: {len(all_listings)}")

        if below:
            logger.info(
                f"Price dropped below ${self.min_price:,} on page {page_num}. "
                f"Collected {len(all_listings)} listings."
            )
            return all_listings

        # Paginate
        while True:
            if self.max_pages > 0 and page_num >= self.max_pages:
                logger.info(f"Reached max_pages limit ({self.max_pages}). Collected {len(all_listings)} listings.")
                break

            page_num += 1
            captured.clear()

            logger.info(f"Loading page {page_num}...")
            try:
                page.goto(
                    f"{CAB_BASE_URL}/past-auctions/?sort=highest_price&page={page_num}",
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception:
                pass
            page.wait_for_timeout(8000 + random.randint(0, 3000))

            if not captured:
                logger.warning(f"No API data on page {page_num}, stopping.")
                break

            data = captured[-1]
            auctions = data.get("auctions", [])
            if not auctions:
                logger.info("No more auctions.")
                break

            below = self._collect_above_threshold(auctions, all_listings)
            logger.info(f"Page {page_num}: {len(auctions)} auctions, kept: {len(all_listings)}")

            if below:
                logger.info(
                    f"Price dropped below ${self.min_price:,} on page {page_num}. "
                    f"Collected {len(all_listings)} listings."
                )
                break

        return all_listings

    def _collect_above_threshold(self, auctions: list[dict], result: list[dict]) -> bool:
        """
        Collect auctions above min_price. Returns True when price drops below threshold.

        Since results are sorted by price descending (sort=9), once we see a price
        below threshold we can stop — all remaining will be lower.
        """
        for a in auctions:
            price = a.get("sale_amount") or a.get("current_bid") or 0
            if price >= self.min_price:
                result.append(a)
            else:
                return True
        return False

    # ── Detail fetching + record building ───────────────────

    def _fetch_details_and_build_records(
        self,
        page: Page,
        context: BrowserContext,
        listings: list[dict],
        cached_details: dict,
    ) -> list[dict]:
        """Fetch detail pages and build normalized records."""
        records = []
        scraped_at = get_scraped_at()
        seen_ids: set[str] = set()
        skipped = {"filter": 0, "dup": 0, "error": 0}

        total = len(listings)
        logger.info(f"Processing {total} listings ({len(cached_details)} cached)...")

        for i, listing in enumerate(listings):
            auction_id = listing.get("id", "")

            # Dedup
            if auction_id in seen_ids:
                skipped["dup"] += 1
                continue
            seen_ids.add(auction_id)

            # Get detail data (cached or fetch)
            if auction_id in cached_details:
                detail = cached_details[auction_id]
            else:
                detail = self._fetch_detail(page, auction_id)
                cached_details[auction_id] = detail

                # Checkpoint every 25 listings
                if (i + 1) % 25 == 0:
                    logger.info(f"Processed {i + 1}/{total} (saving checkpoint)")
                    self._save_checkpoint(listings, cached_details)

            if not detail:
                skipped["error"] += 1
                continue

            # Build record
            record = self._build_record(listing, detail, scraped_at)
            if not record:
                skipped["error"] += 1
                continue

            # Vehicle filter
            if self.vehicle_filter:
                title = record.get("Title", "")
                match = self.vehicle_filter.match(title, "")
                if not match:
                    skipped["filter"] += 1
                    continue

            records.append(record)

        if (i + 1) == total:
            logger.info(f"Processed {total}/{total}")

        for k, v in skipped.items():
            if v:
                logger.info(f"Skipped {v} ({k})")

        return records

    def _fetch_detail(self, page: Page, auction_id: str) -> dict | None:
        """Fetch detail page for a single auction by navigating to it."""
        detail_data = []

        def on_response(response):
            if f"/v2/autos/auctions/{auction_id}" in response.url:
                try:
                    detail_data.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        url = f"{CAB_BASE_URL}/auctions/{auction_id}/"
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass
        page.wait_for_timeout(5000 + random.randint(0, 2000))

        page.remove_listener("response", on_response)

        if detail_data:
            return detail_data[-1]

        logger.warning(f"No detail data for {auction_id}")
        return None

    def _build_record(self, listing: dict, detail: dict, scraped_at: str) -> dict | None:
        """Build a normalized auction record from C&B API data."""
        listing_data = detail.get("listing", {})
        stats = detail.get("stats", {})

        title = unescape(listing_data.get("title") or listing.get("title", ""))
        if not title:
            return None

        # Make & Model — directly from API
        make = listing_data.get("make", "")
        model = listing_data.get("model", "")
        year = listing_data.get("year")

        # VIN
        vin = listing_data.get("vin")

        # Mileage — C&B gives integer directly
        mileage = listing_data.get("mileage")

        # Transmission
        trans_code = listing_data.get("transmission")
        transmission = TRANSMISSION_MAP.get(trans_code)

        # Engine
        engine = normalize_engine_spec(listing_data.get("engine"))

        # Colors
        ext_color = unescape(listing_data.get("exterior_color"))
        int_color = unescape(listing_data.get("interior_color"))

        # Sold status
        status_raw = listing.get("status", "")
        sold_status = STATUS_MAP.get(status_raw, "Unsold")

        # Prices
        selling_price = listing.get("sale_amount") if sold_status == "Sold" else None
        bid_price = listing.get("current_bid") if sold_status == "Unsold" else None

        # Selling date
        auction_end = stats.get("auction_end") or listing.get("auction_end", "")
        selling_date = None
        if auction_end:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", auction_end)
            if m:
                selling_date = m.group(1)

        # Bid count
        bid_count = stats.get("bids")

        # Currency — C&B is USD only
        currency = "USD"
        price_usd = selling_price if selling_price else None
        bid_price_usd = bid_price if bid_price else None
        fx_rate = 1.0 if (selling_price or bid_price) else None

        # Country — extract from location
        location = listing_data.get("location") or listing.get("location", "")
        country = self._location_to_country(location)

        # Image URL
        photo = listing.get("main_photo") or {}
        image_url = None
        if photo.get("base_url") and photo.get("path"):
            image_url = f"https://{photo['base_url']}/{photo['path']}"

        # Listing URL
        slug = f"{year}-{make}-{model}".lower().replace(" ", "-")
        listing_url = f"{CAB_BASE_URL}/auctions/{listing.get('id')}/{slug}"

        # Description — combine sections
        sections = listing_data.get("sections", {})
        description_parts = []
        for key in ("highlights", "equipment", "known_flaws", "modifications",
                     "seller_notes", "ownership_history", "recent_service_history"):
            text = sections.get(key, "")
            if text:
                description_parts.append(text)
        description = "\n\n".join(description_parts) if description_parts else None

        # Authenticity
        authenticity = "Original"
        title_lower = title.lower()
        if any(kw in title_lower for kw in ("re-creation", "recreation", "replica", "evocation")):
            authenticity = "Re-Creation"

        return {
            "Record_ID": listing.get("id", ""),
            "Title": title,
            "Year": year,
            "Make": make,
            "Model": model,
            "Authenticity": authenticity,
            "VIN": vin,
            "Mileage_mi": mileage,
            "Ext_color": ext_color,
            "Int_color": int_color,
            "Engine_spec": engine,
            "Transmission": transmission,
            "Lot_number": None,
            "Estimate_low": None,
            "Estimate_high": None,
            "Estimate_currency": None,
            "Sold_status": sold_status,
            "Currency": currency,
            "Selling_price": selling_price,
            "Selling_price_USD": price_usd,
            "Bid_price": bid_price,
            "Bid_price_USD": bid_price_usd,
            "Bid_count": bid_count,
            "FX_rate_used": fx_rate,
            "Selling_date": selling_date,
            "Auction_house": "Cars & Bids",
            "Country": country,
            "Image_url": image_url,
            "Listing_url": listing_url,
            "Source": "carsandbids.com",
            "Listing_description": unescape(description),
            "Scraped_at": scraped_at,
        }

    @staticmethod
    def _location_to_country(location: str) -> str:
        """Extract country from C&B location (always US/CA format like 'City, ST ZIP')."""
        if not location:
            return "USA"
        # C&B locations are US/CA format: "Loveland, CO 80537" or "Toronto, ON M5V"
        # Check for Canadian provinces
        ca_provinces = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
        parts = location.split(",")
        if len(parts) >= 2:
            state_zip = parts[-1].strip().split()
            if state_zip and state_zip[0].upper() in ca_provinces:
                return "Canada"
        return "USA"

    # ── Checkpoint ──────────────────────────────────────────

    def _load_checkpoint(self) -> dict | None:
        if not self._checkpoint_path.exists():
            return None
        try:
            return json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _save_checkpoint(self, listings: list[dict], details: dict) -> None:
        self._checkpoint_path.write_text(
            json.dumps({"listings": listings, "details": details}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    def _delete_checkpoint(self) -> None:
        if self._checkpoint_path.exists():
            self._checkpoint_path.unlink()
            logger.info("Checkpoint cleaned up")
