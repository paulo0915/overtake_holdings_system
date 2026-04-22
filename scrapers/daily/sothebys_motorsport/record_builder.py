"""Assemble AUCTION_COLUMNS rows — FX, vehicle filter, sold / reserve logic."""

from __future__ import annotations

import logging
from typing import Any

from shared.fx_converter import FXConverter

from .listing_parser import sold_status_from_row

logger = logging.getLogger(__name__)


def _estimate_fields(summary: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    if summary.get("has_reserve") and summary.get("reserve_value") is not None:
        rv = summary["reserve_value"]
        try:
            low = int(rv)
        except (TypeError, ValueError):
            return None, None, None
        cur = summary.get("reserve_currency")
        ec = cur if isinstance(cur, str) else None
        return low, low, ec
    return None, None, None


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict[str, Any]]:
    """``rows`` are merged summaries (listings API + optional detail pageProps)."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = {"filter": 0, "dup": 0}

    for s in rows:
        aid = str(s.get("auction_id") or "")
        if not aid:
            continue
        if aid in seen:
            skipped["dup"] += 1
            continue
        seen.add(aid)

        title = s.get("title") or ""
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        sold_status = sold_status_from_row(s)
        bid_val = s.get("bid_value")
        currency = s.get("currency") or "USD"
        selling_date = None
        sd = s.get("sold_date")
        if isinstance(sd, str) and len(sd) >= 10:
            selling_date = sd[:10]

        selling_price = bid_val if sold_status == "Sold" else None

        est_lo, est_hi, est_cur = _estimate_fields(s)

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

        lot_n = s.get("lot_number")
        lot_out = int(lot_n) if isinstance(lot_n, int) else lot_n

        records.append({
            "Record_ID": aid,
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
            "Lot_number": lot_out,
            "Estimate_low": est_lo,
            "Estimate_high": est_hi,
            "Estimate_currency": est_cur,
            "Sold_status": sold_status,
            "Auction_status": "Completed",
            "Currency": currency,
            "Selling_price": selling_price,
            "Selling_price_USD": selling_price_usd,
            "FX_rate_used": fx_rate,
            "Selling_date": selling_date,
            "Auction_house": "Sotheby's Motorsport",
            "Country": s.get("country"),
            "Image_url": s.get("image_url"),
            "Listing_url": s.get("listing_url"),
            "Source": "sothebysmotorsport.com",
            "Listing_description": s.get("listing_description"),
            "Scraped_at": scraped_at,
        })

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records
