"""Cars.com merged rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

import logging
from typing import Any

from shared.config import MAX_PRICE_USD

logger = logging.getLogger(__name__)


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Detail-first policy: the VDP payload is richer than the SRP row for
    colors, engine, transmission, and images. Fall back to the flat list
    value when detail was missing (rare — listing pulled between fetches).
    """
    detail = detail or {}

    def pick(key: str):
        v = detail.get(key)
        if v is not None and v != "":
            return v
        return flat.get(key)

    vin = pick("vin")
    price = pick("asking_price")

    # Sanity-cap: cars.com sometimes returns a listPrice that is in a
    # different scale than the SRP price (cents instead of dollars, or a
    # wrong field).  Any price above MAX_PRICE_USD is almost certainly a
    # data error; null it so the row is not stored with a garbage price.
    if price is not None and float(price) > MAX_PRICE_USD:
        logger.warning(
            "cars.com price %s exceeds MAX_PRICE_USD=%s — nulling (url=%s)",
            price, MAX_PRICE_USD, flat.get("listing_url", "")[:80],
        )
        price = None

    mileage_mi = pick("mileage_mi")
    image_url = pick("image_url")
    dealer_name = pick("dealer_name")

    return {
        "Record_ID": flat["record_id"],  # dropped by db.py insert, kept for CSV
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": vin,
        "Mileage_mi": mileage_mi,
        "Ext_color": detail.get("ext_color"),
        "Int_color": detail.get("int_color"),
        "Engine_spec": detail.get("engine_spec"),
        "Transmission": detail.get("transmission"),
        "Dealer_name": dealer_name,
        "Asking_price": price,
        "Currency": flat.get("currency") or "USD",
        # Cars.com is USD-native.
        "Asking_price_USD": float(price) if price is not None else None,
        "FX_rate_used": 1.0 if price is not None else None,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "USA",
        "Image_url": image_url,
        "Listing_url": flat.get("listing_url"),
        "Source": "cars.com",
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }
