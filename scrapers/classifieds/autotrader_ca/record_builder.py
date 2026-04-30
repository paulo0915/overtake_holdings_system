"""AutoTrader.ca flat rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

import logging
from typing import Any

from shared.config import MAX_PRICE_USD

logger = logging.getLogger(__name__)


def build_record(
    *,
    flat: dict[str, Any],
    asking_price_usd: float | None,
    fx_rate: float | None,
    scraped_at: str,
    today_iso: str,
    source: str = "autotrader.ca",
    listing_segment: str | None = None,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    For most listings prices are CAD-native; the caller does the CAD→USD
    conversion with the shared ``FXConverter`` and passes the result in.
    When ``flat["price_in_usd_override"]`` is True the dealer declared "PRICE
    IN USD" in their title; Currency is set to USD and FX rate is 1.0.
    """
    price = flat.get("asking_price_cad")
    price_in_usd = flat.get("price_in_usd_override", False)

    if price_in_usd:
        currency = "USD"
        effective_usd = float(price) if price is not None else None
        effective_fx = 1.0
    else:
        currency = "CAD"
        effective_usd = asking_price_usd
        effective_fx = fx_rate

    # Sanity-cap: if the converted USD price is absurdly high it almost
    # certainly reflects a parsing bug (wrong number grabbed from formatted
    # string, or a CAD price labelled as USD).  Null both values so the row
    # is excluded from price-filtered exports rather than stored with garbage.
    if effective_usd is not None and effective_usd > MAX_PRICE_USD:
        logger.warning(
            "autotrader.ca price_usd=%.0f exceeds MAX_PRICE_USD=%s — nulling (url=%s)",
            effective_usd, MAX_PRICE_USD, flat.get("listing_url", "")[:80],
        )
        effective_usd = None
        price = None
        effective_fx = None

    return {
        "Record_ID": flat["record_id"],
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": None,
        "Mileage_mi": flat.get("mileage_mi"),
        "Ext_color": flat.get("ext_color"),
        "Int_color": flat.get("int_color"),
        "Engine_spec": flat.get("engine_spec"),
        "Transmission": flat.get("transmission"),
        "Dealer_name": flat.get("dealer_name"),
        "Asking_price": price,
        "Currency": currency,
        "Asking_price_USD": effective_usd,
        "FX_rate_used": effective_fx,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "Canada",
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": source,
        "Listing_segment": listing_segment,
        "Listing_description": flat.get("description"),
        "Scraped_at": scraped_at,
    }
