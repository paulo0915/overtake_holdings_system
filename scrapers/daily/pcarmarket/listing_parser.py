"""PCarMarket API response parsing — structured features + description field extraction."""

import logging
import re

from bs4 import BeautifulSoup

from shared.normalizer import extract_description_fields, normalize_transmission, unescape

from .config import PCAR_BASE_URL, STATUS_CLASS_MAP

logger = logging.getLogger(__name__)

# Matches standard 17-char VINs and shorter alphanumeric chassis numbers (4–25 chars).
# Strips dots/spaces/hyphens before matching (e.g. "198.042.10002748" → "19804210002748").
_VIN_CLEAN_RE = re.compile(r"[.\s\-]+")
_VIN_ALNUM_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{4,25}$", re.I)
_VIN_PLACEHOLDERS = frozenset({"N/A", "NA", "TBD", "TBC", "NONE", "UNKNOWN", "-", ""})

# Keys the PCarMarket API may use for VIN/chassis — tried in priority order.
_VIN_KEYS = ("vin", "vin_number", "chassis", "chassis_number", "serial", "serial_number",
             "vehicle_vin", "car_vin")

# Keys used for lot number — tried in priority order.
_LOT_KEYS = ("lot_number", "lot_id", "lot", "listing_id", "auction_lot_number", "lot_num")


def _extract_vin(detail: dict) -> str | None:
    """Return VIN/chassis from the detail API response, accepting any valid format."""
    for key in _VIN_KEYS:
        raw = detail.get(key)
        if not raw:
            continue
        s = str(raw).strip()
        if not s or s.upper() in _VIN_PLACEHOLDERS:
            continue
        compact = _VIN_CLEAN_RE.sub("", s).upper()
        if not compact or compact in _VIN_PLACEHOLDERS:
            continue
        # Accept standard alphanumeric chassis (includes purely-numeric pre-1981 serials
        # and short lot-style identifiers like "031" that pcarmarket displays as VIN).
        if _VIN_ALNUM_RE.match(compact) or (compact.isdigit() and 2 <= len(compact) <= 25):
            return compact
        # Relax letter requirement: short mixed strings (e.g. "031") may pass regex but
        # fail _VIN_ALNUM_RE due to length. Accept alphanumeric >= 2 chars.
        if re.match(r"^[A-Z0-9]{2,25}$", compact):
            return compact
    return None


def _extract_lot_number(item: dict, detail: dict) -> str | None:
    """Return lot number from the API response."""
    for src in (detail, item):
        for key in _LOT_KEYS:
            val = src.get(key)
            if val and str(val).strip():
                return str(val).strip()
    return None


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
    vin = _extract_vin(detail)
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

    lot_number = _extract_lot_number(item, detail)

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
        "lot_number_raw": lot_number,
    }
