"""Worldwide Auctioneers lots -> ``AUCTION_COLUMNS``."""

from __future__ import annotations

import logging
from typing import Any

from shared.fx_converter import FXConverter

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
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sold = bool(m.get("sold"))
        price = m.get("price")          # int USD or None
        price_disclosed = bool(m.get("price_disclosed"))
        currency = (m.get("currency") or "USD").strip() or "USD"

        # Sold status wording
        if sold and price:
            sold_status = "Sold"
        elif sold and price_disclosed:
            sold_status = "Sold"          # sold but price withheld
        elif sold:
            sold_status = "Sold"
        else:
            sold_status = "Unsold"

        selling_price = price if sold else None
        bid_price = price if not sold and price else None
        selling_date = m.get("selling_date")

        selling_price_usd = bid_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )
        elif bid_price is not None:
            bid_price_usd, fx_rate = await fx_converter.convert_to_usd(
                bid_price, currency, selling_date,
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
                "Ext_color": None,
                "Int_color": None,
                "Engine_spec": None,
                "Transmission": None,
                "Lot_number": m.get("lot"),
                "Estimate_low": None,
                "Estimate_high": None,
                "Estimate_currency": None,
                "Sold_status": sold_status,
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "Bid_price": bid_price,
                "Bid_price_USD": bid_price_usd,
                "Bid_count": None,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Worldwide Auctioneers",
                "Country": "USA",
                "Image_url": m.get("image_url"),
                "Listing_url": None,       # no per-lot URLs in POST response
                "Source": "listings.worldwideauctioneers.com",
                "Listing_description": event or None,
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
