"""AutoScout24 list + detail → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared.fx_converter import FXConverter

logger = logging.getLogger(__name__)


async def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    fx: FXConverter,
    scraped_at: str,
    today_iso: str,
    listing_segment: str | None,
) -> dict[str, Any]:
    """Assemble one classified row; converts asking price to USD via Frankfurter."""
    detail = detail or {}

    def pick(key: str):
        v = detail.get(key)
        if v is not None and v != "":
            return v
        return flat.get(key)

    price = flat.get("asking_price")
    currency = (flat.get("currency") or "EUR").upper()
    if "asking_price" in detail:
        price = detail["asking_price"]
    if detail.get("currency"):
        currency = str(detail["currency"]).upper()

    price_usd, fx_rate = (None, None)
    if price is not None:
        price_usd, fx_rate = await fx.convert_to_usd(float(price), currency, today_iso)

    year = pick("year")
    mileage_mi = pick("mileage_mi")
    if mileage_mi is None and flat.get("mileage_km_hint") is not None:
        km = flat["mileage_km_hint"]
        mileage_mi = int(round(km * 0.621371))

    seg = listing_segment if listing_segment and listing_segment != "all" else None

    return {
        "Record_ID": flat["record_id"],
        "Title": flat.get("title"),
        "Year": year,
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": pick("vin"),
        "Mileage_mi": mileage_mi,
        "Ext_color": detail.get("ext_color"),
        "Int_color": detail.get("int_color"),
        "Engine_spec": detail.get("engine_spec"),
        "Transmission": pick("transmission") or flat.get("transmission_hint"),
        "Dealer_name": pick("dealer_name") or flat.get("dealer_name"),
        "Asking_price": price,
        "Currency": currency,
        "Asking_price_USD": price_usd,
        "FX_rate_used": fx_rate,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": pick("country") or flat.get("country"),
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": "autoscout24.com",
        "Listing_segment": seg,
        "Listing_description": detail.get("listing_description"),
        "Scraped_at": scraped_at,
    }
