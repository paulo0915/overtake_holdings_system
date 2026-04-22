"""C&B JSON API response → normalized auction record fields."""

import re
import logging

from shared.normalizer import normalize_engine_spec, unescape

from .config import CAB_BASE_URL, STATUS_MAP, TRANSMISSION_MAP

logger = logging.getLogger(__name__)

# Canadian province codes — used to distinguish CA listings from US ones.
_CA_PROVINCES = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})


def location_to_country(location: str) -> str:
    """Infer country from a C&B location string ('City, ST ZIP' format)."""
    if not location:
        return "USA"
    parts = location.split(",")
    if len(parts) >= 2:
        state_zip = parts[-1].strip().split()
        if state_zip and state_zip[0].upper() in _CA_PROVINCES:
            return "Canada"
    return "USA"


def build_record(listing: dict, detail: dict, scraped_at: str) -> dict | None:
    """
    Extract, normalize and assemble a single C&B auction record.

    Returns None if the listing has no usable title (invalid / empty entry).
    """
    listing_data = detail.get("listing", {})
    stats = detail.get("stats", {})

    title = unescape(listing_data.get("title") or listing.get("title", ""))
    if not title:
        return None

    make = listing_data.get("make", "")
    model = listing_data.get("model", "")
    year = listing_data.get("year")
    vin = listing_data.get("vin")
    mileage = listing_data.get("mileage")

    trans_code = listing_data.get("transmission")
    transmission = TRANSMISSION_MAP.get(trans_code)

    engine = normalize_engine_spec(listing_data.get("engine"))
    ext_color = unescape(listing_data.get("exterior_color"))
    int_color = unescape(listing_data.get("interior_color"))

    status_raw = listing.get("status", "")
    sold_status = STATUS_MAP.get(status_raw, "Unsold")

    selling_price = listing.get("sale_amount") if sold_status == "Sold" else None
    bid_price = listing.get("current_bid") if sold_status == "Unsold" else None

    auction_end = stats.get("auction_end") or listing.get("auction_end", "")
    selling_date = None
    if auction_end:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", auction_end)
        if m:
            selling_date = m.group(1)

    bid_count = stats.get("bids")

    # C&B is USD-only; FX rate is always 1.0 when a price is present.
    currency = "USD"
    price_usd = selling_price
    bid_price_usd = bid_price
    fx_rate = 1.0 if (selling_price is not None or bid_price is not None) else None

    location = listing_data.get("location") or listing.get("location", "")
    country = location_to_country(location)

    photo = listing.get("main_photo") or {}
    image_url = None
    if photo.get("base_url") and photo.get("path"):
        image_url = f"https://{photo['base_url']}/{photo['path']}"

    slug = f"{year}-{make}-{model}".lower().replace(" ", "-")
    listing_url = f"{CAB_BASE_URL}/auctions/{listing.get('id')}/{slug}"

    sections = listing_data.get("sections", {})
    description_parts = [
        sections.get(key, "")
        for key in (
            "highlights", "equipment", "known_flaws", "modifications",
            "seller_notes", "ownership_history", "recent_service_history",
        )
        if sections.get(key)
    ]
    description = "\n\n".join(description_parts) if description_parts else None

    return {
        "Record_ID": str(listing.get("id", "")),
        "Title": title,
        "Year": year,
        "Make": make,
        "Model": model,
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
        "Auction_status": "Completed",
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
