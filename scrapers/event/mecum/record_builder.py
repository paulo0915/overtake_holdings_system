"""Mecum — merged Algolia + detail rows → ``AUCTION_COLUMNS``."""

import logging
from typing import Any

from shared.fx_converter import FXConverter
from shared.normalizer import is_non_car_make

from .listing_parser import enrich_vin_and_transmission_from_description, merge_hit_and_detail

logger = logging.getLogger(__name__)


async def build_records(
    summaries: list[dict[str, Any]],
    details: dict[str, dict | None],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    skipped = {"filter": 0, "dup": 0}
    seen: set[str] = set()

    for s in summaries:
        wid = str(s.get("web_id") or "").strip()
        if not wid:
            continue
        if wid in seen:
            skipped["dup"] += 1
            continue
        seen.add(wid)

        title = (s.get("title") or "").strip()
        if is_non_car_make(s.get("make")):
            skipped["filter"] += 1
            continue
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        d = details.get(wid)
        m = merge_hit_and_detail(s, d)

        base_desc = m.get("listing_description")
        enrich_vin_and_transmission_from_description(m, base_desc)

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
                "Record_ID": wid,
                "Title": m.get("title") or title,
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
                # Intentionally blank for Mecum exports (pre-sale estimates are not hammer results).
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
                "Auction_house": "Mecum",
                "Auction_event": m.get("auction_event"),
                "Country": "USA",
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "mecum.com",
                "Listing_description": base_desc,
                "Scraped_at": scraped_at,
            }
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
