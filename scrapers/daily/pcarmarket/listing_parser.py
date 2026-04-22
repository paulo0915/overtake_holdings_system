"""PCarMarket API response parsing — structured features + description field extraction."""

import logging
import re

from bs4 import BeautifulSoup

from shared.normalizer import extract_description_fields, normalize_transmission, unescape

from .config import PCAR_BASE_URL, STATUS_CLASS_MAP

logger = logging.getLogger(__name__)


def parse(item: dict, detail: dict) -> dict:
    """
    Extract and normalize all fields from a PCarMarket API item + detail response.

    Returns an intermediate dict with all field values ready for record assembly.
    FX conversion and final AUCTION_COLUMNS mapping are handled by record_builder.
    """
    vehicle = item.get("vehicle", {})

    title = unescape(item.get("title", ""))
    make = vehicle.get("make") or None
    model = vehicle.get("model") or None
    year = vehicle.get("year")
    vin = detail.get("vin") or None
    mileage = item.get("mileage_body")

    status_class = item.get("status_class", "")
    sold_status = STATUS_CLASS_MAP.get(status_class, "Unsold")

    high_bid = item.get("high_bid")
    final_bid = detail.get("auction_final_bid")
    price = None
    if high_bid is not None:
        price = int(float(high_bid))
    elif final_bid is not None:
        price = int(float(final_bid))

    selling_price = price if sold_status == "Sold" else None

    end_date = item.get("end_date", "")
    selling_date = None
    if end_date:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", end_date)
        if m:
            selling_date = m.group(1)
    country = detail.get("country") or "USA"
    image_url = item.get("featured_image_large_url") or item.get("featured_image_url")
    currency = item.get("currency") or detail.get("currency") or "USD"

    slug = item.get("slug", "")
    listing_url = f"{PCAR_BASE_URL}/auction/{slug}"

    # Structured features (primary source for transmission and exterior color)
    features = {
        f["title"]: f["description"]
        for f in detail.get("features", [])
        if f.get("title")
    }
    transmission = normalize_transmission(features.get("Transmission"))
    ext_color = features.get("Color") or None

    # Description HTML → plain text (secondary source for engine, int_color, fallbacks)
    description_html = detail.get("description", "")
    description = None
    if description_html:
        description = BeautifulSoup(description_html, "lxml").get_text(" ", strip=True)

    desc_fields = extract_description_fields(description or "")
    engine = desc_fields["engine"]
    if not transmission:
        transmission = desc_fields["transmission"]
    if not ext_color:
        ext_color = desc_fields["ext_color"]
    int_color = desc_fields["int_color"]

    return {
        "item_id": item.get("id"),
        "slug": slug,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "vin": vin,
        "mileage": mileage,
        "ext_color": ext_color,
        "int_color": int_color,
        "engine": engine,
        "transmission": transmission,
        "sold_status": sold_status,
        "selling_price": selling_price,
        "selling_date": selling_date,
        "currency": currency,
        "country": country,
        "image_url": image_url,
        "listing_url": listing_url,
        "listing_description": description,
        "lot_number_raw": detail.get("lot_number") or None,
    }
