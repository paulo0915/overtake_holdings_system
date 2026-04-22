"""Gooding & Company merged rows -> ``AUCTION_COLUMNS``."""

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
    """Sold-only: drop non-Car lots, unsold, and sold-but-no-salePrice."""
    records: list[dict[str, Any]] = []
    skipped = {"filter": 0, "dup": 0, "non_car": 0, "unsold": 0, "sold_no_price": 0}
    seen: set[str] = set()

    for m in rows:
        rid = str(m.get("record_id") or "").strip()
        if not rid:
            continue
        if rid in seen:
            skipped["dup"] += 1
            continue
        seen.add(rid)

        # Contentful ``__typename`` is the authoritative flag:
        #   ``ContentfulVehicle``    — cars (keep)
        #   ``ContentfulAutomobilia`` — memorabilia/signs/models (drop)
        #   ``ContentfulMotorcycle``  / similar (drop)
        typename = (m.get("item_typename") or "").strip()
        if typename != "ContentfulVehicle":
            skipped["non_car"] += 1
            continue
        item_type = (m.get("item_type") or "").strip().lower()
        if item_type and item_type != "car":
            skipped["non_car"] += 1
            continue

        title = (m.get("title") or "").strip()
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sale_price = m.get("sale_price")
        # Gooding has no explicit status field — salePrice presence means it sold.
        if sale_price in (None, 0):
            skipped["unsold"] += 1
            continue

        currency = (m.get("currency") or "USD").strip().upper() or "USD"
        selling_date = m.get("selling_date")

        selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
            sale_price, currency, selling_date,
        )

        has_est = m.get("estimate_low") is not None or m.get("estimate_high") is not None

        records.append(
            {
                "Record_ID": rid,
                "Title": m.get("title") or None,
                "Year": m.get("year"),
                "Make": m.get("make"),
                "Model": m.get("model"),
                "VIN": m.get("vin"),
                "Mileage_mi": m.get("mileage_mi"),
                "Ext_color": None,
                "Int_color": None,
                "Engine_spec": m.get("engine_spec"),
                "Transmission": m.get("transmission"),
                "Lot_number": m.get("lot_number"),
                "Estimate_low": m.get("estimate_low"),
                "Estimate_high": m.get("estimate_high"),
                "Estimate_currency": currency if has_est else None,
                "Sold_status": "Sold",
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": sale_price,
                "Selling_price_USD": selling_price_usd,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Gooding & Company",
                "Country": None,
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "goodingco.com",
                "Listing_description": m.get("description"),
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
