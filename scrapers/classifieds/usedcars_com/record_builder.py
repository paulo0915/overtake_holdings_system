"""UsedCars.com merged rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Merge policy: list tile is the primary source (it already has VIN,
    price, colors, transmission, dealer name). Detail contributes
    ``sellerNotes`` (description) and a synthesized ``engine_spec``; used
    as fallback for any tile field that was empty.
    """
    detail = detail or {}

    def fallback(key: str):
        f = flat.get(key)
        if f is not None and f != "":
            return f
        return detail.get(key)

    price = flat.get("asking_price")

    return {
        "Record_ID": flat["record_id"],  # dropped by db.py insert, kept for CSV
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": flat.get("vin"),
        "Mileage_mi": flat.get("mileage_mi"),
        "Ext_color": fallback("ext_color"),
        "Int_color": fallback("int_color"),
        "Engine_spec": detail.get("engine_spec"),
        "Transmission": fallback("transmission"),
        "Dealer_name": fallback("dealer_name"),
        "Asking_price": price,
        "Currency": flat.get("currency") or "USD",
        # UsedCars.com is USD-native.
        "Asking_price_USD": float(price) if price is not None else None,
        "FX_rate_used": 1.0 if price is not None else None,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "USA",
        "Image_url": fallback("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": "usedcars.com",
        "Listing_segment": "used",  # UsedCars.com is a used-only feed
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }
