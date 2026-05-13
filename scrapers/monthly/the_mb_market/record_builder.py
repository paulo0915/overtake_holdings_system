"""Assemble ``AUCTION_COLUMNS`` rows for The MB Market."""

from __future__ import annotations

import logging
import re
from typing import Any

from shared.fx_converter import FXConverter
from shared.normalizer import is_non_car_make

logger = logging.getLogger(__name__)

# Titles containing these phrases are automobilia / toys / parts — not real cars.
_NON_CAR_TITLE_RE = re.compile(
    r"\b(pedal\s+car|toy\s+car|scale\s+model|automobilia|memorabilia"
    r"|model\s+car|slot\s+car|rc\s+car|diecast|miniature\s+car"
    r"|poster|artwork|brochure|parts\s+lot|wheels?\s+lot)\b",
    re.IGNORECASE,
)


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict[str, Any]]:
    """``rows`` are merged summaries (AJAX card + detail page)."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = {"filter": 0, "dup": 0, "non_car": 0}

    for s in rows:
        rid = str(s.get("record_id") or "")
        if not rid:
            continue
        if rid in seen:
            skipped["dup"] += 1
            continue
        seen.add(rid)

        title = s.get("title") or ""
        # Drop automobilia / parts / wheels — MBM mixes them into its results.
        # Real car listings always start with a 4-digit model year. Parts titles
        # read like ``Pair of 17" AMG Aero II Monoblock Wheels for W124``.
        if not s.get("year"):
            skipped["non_car"] += 1
            continue
        if is_non_car_make(s.get("make")):
            skipped["non_car"] += 1
            continue
        if _NON_CAR_TITLE_RE.search(title):
            skipped["non_car"] += 1
            continue
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sold_status = s.get("sold_status") or "Unsold"
        amt = s.get("amount_usd")
        currency = s.get("currency") or "USD"
        selling_date = s.get("selling_date")

        selling_price = amt if sold_status == "Sold" else None

        selling_price_usd = fx_rate = None
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

        records.append(
            {
                "Record_ID": rid,
                "Title": title,
                "Year": s.get("year"),
                "Make": "Mercedes-Benz",
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
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "The MB Market",
                "Auction_event": None,
                "Country": s.get("country") or "USA",
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
