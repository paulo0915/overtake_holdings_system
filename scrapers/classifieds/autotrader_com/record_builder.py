"""AutoTrader.com flat rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
    source: str = "autotrader.com",
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``."""
    detail = detail or {}

    price = flat.get("asking_price")

    return {
        "Record_ID": flat["record_id"],
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": flat.get("vin"),
        "Mileage_mi": flat.get("mileage_mi"),
        "Ext_color": flat.get("ext_color"),
        "Int_color": flat.get("int_color"),
        "Engine_spec": flat.get("engine_spec"),
        "Transmission": flat.get("transmission"),
        "Dealer_name": flat.get("dealer_name"),
        "Asking_price": price,
        "Currency": flat.get("currency") or "USD",
        # AutoTrader.com is USD-native.
        "Asking_price_USD": float(price) if price is not None else None,
        "FX_rate_used": 1.0 if price is not None else None,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "USA",
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": source,
        "Listing_segment": "new" if flat.get("is_new") else "used",
        # Prefer the VDP's ``fullDescription`` (the "More from the seller"
        # body). Fall back to the list-row's ``description.label`` (short,
        # ~143 chars) when the VDP has no extended notes — better than
        # leaving the field blank.
        "Listing_description": detail.get("description") or flat.get("description_short"),
        "Scraped_at": scraped_at,
    }
