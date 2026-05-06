"""Broad Arrow vehicle rows -> ``AUCTION_COLUMNS``."""

import logging
import re
from typing import Any

from shared.fx_converter import FXConverter
from shared.normalizer import is_non_car_make

logger = logging.getLogger(__name__)

# Extract event slug from listing_url: "/vehicles/ve26_r0025/..." → "ve26"
_EVENT_SLUG_RE = re.compile(r"/vehicles/([a-z0-9]+)_r\d+", re.IGNORECASE)


def _event_slug(url: str) -> str | None:
    m = _EVENT_SLUG_RE.search(url or "")
    return m.group(1).lower() if m else None


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
    *,
    mode: str = "past",
    event_meta: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """``mode='past'`` = hammer-based rows; ``mode='upcoming'`` = estimate-based rows."""
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

        # Resolve event metadata (country, currency, date) from the cached lookup.
        # Falls back to the JSON-LD currency if the event metadata is unavailable.
        slug = _event_slug(m.get("listing_url") or "")
        ev = (event_meta or {}).get(slug or "", {}) if slug else {}
        country = ev.get("country") or "USA"
        currency = (
            ev.get("currency")
            or (m.get("currency") or "").strip()
            or "USD"
        )
        selling_date = m.get("selling_date") or ev.get("date")

        if mode == "upcoming":
            records.append(_build_upcoming_row(m, currency, selling_date, scraped_at, country=country))
            continue

        hammer = m.get("hammer")
        sold_status = (m.get("sold_status") or "Unsold").strip() or "Unsold"
        selling_price = hammer if sold_status == "Sold" and hammer is not None else None

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
                "Estimate_low": m.get("estimate_low"),
                "Estimate_high": m.get("estimate_high"),
                "Estimate_currency": currency if (m.get("estimate_low") or m.get("estimate_high")) else None,
                "Sold_status": sold_status,
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "Broad Arrow",
                "Auction_event": m.get("auction_event"),
                "Country": country,
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "broadarrowauctions.com",
                "Listing_description": m.get("listing_description"),
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records


def _build_upcoming_row(m, currency, selling_date, scraped_at, *, country="USA"):
    """Upcoming: empty Sold_status, no hammer/bid, estimate range is the signal."""
    has_est = m.get("estimate_low") is not None or m.get("estimate_high") is not None
    return {
        "Record_ID": str(m.get("record_id") or ""),
        "Title": (m.get("title") or "").strip() or None,
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
        "Estimate_low": m.get("estimate_low"),
        "Estimate_high": m.get("estimate_high"),
        "Estimate_currency": currency if has_est else None,
        "Sold_status": m.get("sold_status"),
        "Auction_status": "Upcoming",
        "Currency": currency,
        "Selling_price": None,
        "Selling_price_USD": None,
        "FX_rate_used": None,
        "Selling_date": selling_date,
        "Auction_house": "Broad Arrow",
        "Auction_event": m.get("auction_event"),
        "Country": country,
        "Image_url": m.get("image_url"),
        "Listing_url": m.get("listing_url"),
        "Source": "broadarrowauctions.com",
        "Listing_description": m.get("listing_description"),
        "Scraped_at": scraped_at,
    }
