"""Barrett-Jackson docket rows → ``AUCTION_COLUMNS``."""

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
    """Build canonical AUCTION_COLUMNS records from parsed BJ rows.

    ``mode='past'``     — sold-only rows; Auction_status='Completed'.
    ``mode='upcoming'`` — preview lots; Auction_status='Upcoming',
                         Selling_price/FX blank, Estimate columns also
                         blank (Barrett-Jackson doesn't publish pre-sale
                         estimates), Selling_date is the future run date.
    """
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

        if mode == "upcoming":
            records.append(_build_upcoming_row(m, rid, title, scraped_at))
            continue

        hammer = m.get("hammer_usd")
        sold_status = (m.get("sold_status") or "Sold").strip() or "Sold"
        selling_price = hammer if sold_status == "Sold" else None

        selling_date = m.get("selling_date")
        currency = "USD"

        selling_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )

        records.append(
            {
                "Record_ID": rid,
                "Title": m.get("title") or title or None,
                "Year": m.get("year"),
                "Make": m.get("make"),
                "Model": m.get("model"),
                "VIN": m.get("vin"),
                "Mileage_mi": m.get("mileage_mi"),
                "Ext_color": m.get("ext_color"),
                "Int_color": m.get("int_color"),
                "Engine_spec": m.get("engine_spec"),
                "Transmission": m.get("transmission"),
                "Lot_number": m.get("lot_number"),
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
                "Auction_house": "Barrett-Jackson",
                "Auction_event": m.get("auction_event"),
                "Country": "USA",
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "barrett-jackson.com",
                "Listing_description": m.get("listing_description"),
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records


def _build_upcoming_row(
    m: dict[str, Any], rid: str, title: str, scraped_at: str,
) -> dict[str, Any]:
    """One upcoming-lot row. No hammer/estimate — BJ doesn't publish
    pre-sale estimates, only reserve type and reserve amount (rarely > 0)."""
    return {
        "Record_ID": rid,
        "Title": m.get("title") or title or None,
        "Year": m.get("year"),
        "Make": m.get("make"),
        "Model": m.get("model"),
        "VIN": m.get("vin"),
        "Mileage_mi": m.get("mileage_mi"),
        "Ext_color": m.get("ext_color"),
        "Int_color": m.get("int_color"),
        "Engine_spec": m.get("engine_spec"),
        "Transmission": m.get("transmission"),
        "Lot_number": m.get("lot_number"),
        "Estimate_low": None,
        "Estimate_high": None,
        "Estimate_currency": None,
        "Sold_status": None,
        "Auction_status": "Upcoming",
        "Currency": "USD",
        "Selling_price": None,
        "Selling_price_USD": None,
        "FX_rate_used": None,
        "Selling_date": m.get("selling_date"),  # future run_date
        "Auction_house": "Barrett-Jackson",
        "Auction_event": m.get("auction_event"),
        "Country": "USA",
        "Image_url": m.get("image_url"),
        "Listing_url": m.get("listing_url"),
        "Source": "barrett-jackson.com",
        "Listing_description": m.get("listing_description"),
        "Scraped_at": scraped_at,
    }
