"""Assemble ``AUCTION_COLUMNS`` rows for The MB Market."""

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
    """``rows`` are merged summaries (AJAX card + detail page)."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = {"filter": 0, "dup": 0}

    for s in rows:
        rid = str(s.get("record_id") or "")
        if not rid:
            continue
        if rid in seen:
            skipped["dup"] += 1
            continue
        seen.add(rid)

        title = s.get("title") or ""
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sold_status = s.get("sold_status") or "Unsold"
        amt = s.get("amount_usd")
        currency = s.get("currency") or "USD"
        selling_date = s.get("selling_date")

        selling_price = amt if sold_status == "Sold" else None
        bid_price = amt if sold_status == "Unsold" else None

        selling_price_usd = bid_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )
            if selling_price_usd is None and currency != "USD":
                logger.warning(
                    "FX conversion failed: %r (%s→USD, date=%s)",
                    title[:50],
                    currency,
                    selling_date,
                )
        elif bid_price is not None:
            bid_price_usd, fx_rate = await fx_converter.convert_to_usd(
                bid_price, currency, selling_date,
            )
            if bid_price_usd is None and currency != "USD":
                logger.warning(
                    "FX conversion failed: %r (%s→USD, date=%s)",
                    title[:50],
                    currency,
                    selling_date,
                )

        records.append(
            {
                "Record_ID": rid,
                "Title": title,
                "Year": s.get("year"),
                "Make": s.get("make"),
                "Model": s.get("model"),
                "VIN": s.get("vin"),
                "Mileage_mi": s.get("mileage_mi"),
                "Ext_color": s.get("ext_color"),
                "Int_color": s.get("int_color"),
                "Engine_spec": s.get("engine_spec"),
                "Transmission": s.get("transmission"),
                "Lot_number": s.get("lot_number"),
                "Estimate_low": None,
                "Estimate_high": None,
                "Estimate_currency": None,
                "Sold_status": sold_status,
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "Bid_price": bid_price,
                "Bid_price_USD": bid_price_usd,
                "Bid_count": s.get("bid_count"),
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "The MB Market",
                "Country": s.get("country"),
                "Image_url": s.get("image_url"),
                "Listing_url": s.get("listing_url"),
                "Source": "thembmarket.com",
                "Listing_description": s.get("listing_description"),
                "Scraped_at": scraped_at,
            }
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
