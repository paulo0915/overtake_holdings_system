"""Bonhams Cars merged rows -> ``AUCTION_COLUMNS``."""

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
    *,
    mode: str = "past",
) -> list[dict[str, Any]]:
    """``mode='past'`` = sold-only with hammer price; ``mode='upcoming'`` = all car lots."""
    records: list[dict[str, Any]] = []
    skipped = {"filter": 0, "dup": 0, "unsold": 0, "sold_no_price": 0}
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

        currency = (m.get("currency") or "USD").strip().upper() or "USD"
        selling_date = _date_only(m.get("hammer_time"))
        has_est = m.get("estimate_low") is not None or m.get("estimate_high") is not None

        if mode == "upcoming":
            records.append({
                "Record_ID": rid,
                "Title": m.get("title") or None,
                "Year": m.get("year"),
                "Make": m.get("make"),
                "Model": m.get("model"),
                "VIN": m.get("vin"),
                "Mileage_mi": m.get("mileage_mi"),
                "Ext_color": m.get("ext_color"),
                "Int_color": m.get("int_color"),
                "Engine_spec": m.get("engine_spec"),
                "Transmission": m.get("transmission"),
                "Lot_number": m.get("lot_no"),
                "Estimate_low": m.get("estimate_low"),
                "Estimate_high": m.get("estimate_high"),
                "Estimate_currency": currency if has_est else None,
                "Sold_status": None,
                "Auction_status": "Upcoming",
                "Currency": currency,
                "Selling_price": None,
                "Selling_price_USD": None,
                "FX_rate_used": None,
                "Selling_date": selling_date,
                "Auction_house": "Bonhams",
                "Auction_event": m.get("auction_title"),
                "Country": m.get("country"),
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "cars.bonhams.com",
                "Listing_description": m.get("description"),
                "Scraped_at": scraped_at,
            })
            continue

        status = (m.get("status") or "").upper()
        hammer_price = m.get("hammer_price")
        # Sold + price required.
        if status != "SOLD":
            skipped["unsold"] += 1
            continue
        if hammer_price is None or hammer_price == 0:
            skipped["sold_no_price"] += 1
            continue

        selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
            hammer_price, currency, selling_date,
        )

        records.append(
            {
                "Record_ID": rid,
                "Title": m.get("title") or None,
                "Year": m.get("year"),
                "Make": m.get("make"),
                "Model": m.get("model"),
                "VIN": m.get("vin"),
                "Mileage_mi": m.get("mileage_mi"),
                "Ext_color": m.get("ext_color"),
                "Int_color": m.get("int_color"),
                "Engine_spec": m.get("engine_spec"),
                "Transmission": m.get("transmission"),
                "Lot_number": m.get("lot_no"),
                "Estimate_low": m.get("estimate_low"),
                "Estimate_high": m.get("estimate_high"),
                "Estimate_currency": currency if has_est else None,
                "Sold_status": "Sold",
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": hammer_price,
                "Selling_price_USD": selling_price_usd,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Bonhams",
                "Auction_event": m.get("auction_title"),
                "Country": m.get("country"),
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "cars.bonhams.com",
                "Listing_description": m.get("description"),
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records


def _date_only(dt: str | None) -> str | None:
    """``2026-01-30T10:00:00+00:00`` → ``2026-01-30``."""
    if not dt:
        return None
    s = str(dt)
    return s[:10] if len(s) >= 10 else None
