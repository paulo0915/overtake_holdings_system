"""TrueCar merged rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
    source: str = "truecar.com",
    listing_segment: str | None = None,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Merge policy: list is the primary source for structured fields (it
    already has colors, engine, transmission, VIN, price, mileage, dealer,
    image). Detail contributes the seller-written ``description`` and is
    used as a fallback for colors / engine / transmission / mileage when
    the list row was empty for those.
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
        "VIN": fallback("vin"),
        "Mileage_mi": fallback("mileage_mi"),
        "Ext_color": fallback("ext_color"),
        "Int_color": fallback("int_color"),
        "Engine_spec": fallback("engine_spec"),
        "Transmission": fallback("transmission"),
        "Dealer_name": flat.get("dealer_name"),
        "Asking_price": price,
        "Currency": flat.get("currency") or "USD",
        # TrueCar is USD-native.
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
        "Listing_segment": listing_segment,
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }
