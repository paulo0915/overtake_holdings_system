"""AutoTrader.ca flat rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    asking_price_usd: float | None,
    fx_rate: float | None,
    scraped_at: str,
    today_iso: str,
    source: str = "autotrader.ca",
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Prices are CAD-native; the caller does the CAD→USD conversion with the
    shared ``FXConverter`` and passes the result in.
    """
    price_cad = flat.get("asking_price_cad")

    return {
        "Record_ID": flat["record_id"],
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        # No VIN exposed in the feed; leave empty so the schema column remains
        # honest rather than filled with the AutoScout24 UUID.
        "VIN": None,
        "Mileage_mi": flat.get("mileage_mi"),
        "Ext_color": flat.get("ext_color"),
        "Int_color": flat.get("int_color"),
        "Engine_spec": flat.get("engine_spec"),
        "Transmission": flat.get("transmission"),
        "Dealer_name": flat.get("dealer_name"),
        "Asking_price": price_cad,
        "Currency": "CAD",
        "Asking_price_USD": asking_price_usd,
        "FX_rate_used": fx_rate,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "Canada",
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": source,
        "Listing_description": flat.get("description"),
        "Scraped_at": scraped_at,
    }
