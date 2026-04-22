"""Bonhams Cars merged rows -> ``AUCTION_COLUMNS``."""

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
    """Sold-only: drop unsold and sold-but-price-undisclosed entries."""
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
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
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

        currency = (m.get("currency") or "USD").strip().upper() or "USD"
        selling_date = _date_only(m.get("hammer_time"))

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
                "Ext_color": None,
                "Int_color": None,
                "Engine_spec": None,
                "Transmission": None,
                "Lot_number": m.get("lot_no"),
                "Estimate_low": m.get("estimate_low"),
                "Estimate_high": m.get("estimate_high"),
                "Estimate_currency": currency if (m.get("estimate_low") or m.get("estimate_high")) else None,
                "Sold_status": "Sold",
                "Currency": currency,
                "Selling_price": hammer_price,
                "Selling_price_USD": selling_price_usd,
                "Bid_price": None,
                "Bid_price_USD": None,
                "Bid_count": None,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Bonhams",
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
