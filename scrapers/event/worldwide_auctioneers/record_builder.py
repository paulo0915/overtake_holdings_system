"""Worldwide Auctioneers lots -> ``AUCTION_COLUMNS``."""

from __future__ import annotations

import logging
from typing import Any

from shared.fx_converter import FXConverter
from shared.normalizer import is_non_car_make

logger = logging.getLogger(__name__)


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    skipped = {"filter": 0, "dup": 0}
    seen: set[str] = set()

    for m in rows:
        rid = str(m.get("record_id") or "").strip()
        if not rid:
            continue
        if rid in seen:
            skipped["dup"] += 1
            continue
        seen.add(rid)

        title = (m.get("title") or "").strip()
        if is_non_car_make(m.get("make")):
            skipped["filter"] += 1
            continue
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sold = bool(m.get("sold"))
        price = m.get("price")          # int USD or None (undisclosed when None)
        currency = (m.get("currency") or "USD").strip() or "USD"

        # Keep sold lots even when hammer is undisclosed (empty Selling_price),
        # because this source explicitly exposes "SOLD" without always exposing price.
        if not sold:
            skipped.setdefault("unsold", 0)
            skipped["unsold"] += 1
            continue

        selling_price = price
        sold_status = "Sold" if selling_price is not None else "Sold - Price Undisclosed"
        selling_date = m.get("selling_date")

        selling_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )

        event = (m.get("auction_event") or "").strip()

        records.append(
            {
                "Record_ID": rid,
                "Title": title or None,
                "Year": m.get("year") or None,
                "Make": m.get("make"),
                "Model": m.get("model"),
                "VIN": m.get("vin"),
                "Mileage_mi": m.get("mileage_mi"),
                "Ext_color": m.get("ext_color"),
                "Int_color": m.get("int_color"),
                "Engine_spec": m.get("engine_spec"),
                "Transmission": m.get("transmission"),
                "Lot_number": m.get("lot"),
                "Estimate_low": None,
                "Estimate_high": None,
                "Estimate_currency": None,
                "Sold_status": sold_status,
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Worldwide Auctioneers",
                "Country": "USA",
                "Image_url": m.get("image_url"),
                "Listing_url": f"https://listings.worldwideauctioneers.com/inventory/#lot-{rid}",
                "Source": "listings.worldwideauctioneers.com",
                "Listing_description": event or None,
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
