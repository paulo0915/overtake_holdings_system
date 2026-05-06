"""RM Sotheby's merged rows -> ``AUCTION_COLUMNS``."""

import logging
import re
from typing import Any

from shared.fx_converter import FXConverter
from shared.normalizer import canonical_country_name, is_non_car_make

logger = logging.getLogger(__name__)

# US-state tail inside a location string like ``Los Angeles, California`` means
# the car is in the US — bypass the generic country-name override table since
# the tail won't be a country.
_US_STATE_RE = re.compile(
    r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    r"Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    r"Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    r"West Virginia|Wisconsin|Wyoming)\b",
    re.I,
)


def _country_from_location(loc: str | None) -> str | None:
    if not loc:
        return None
    s = loc.strip()
    if _US_STATE_RE.search(s):
        return "USA"
    tail = s.rsplit(",", 1)[-1].strip() or None
    return canonical_country_name(tail)


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
    *,
    mode: str = "past",
) -> list[dict[str, Any]]:
    """``mode='past'`` = sold-only; ``mode='upcoming'`` = all lots with ``Sold_status='Upcoming'``."""
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

        currency = (m.get("currency") or "USD").strip() or "USD"
        selling_date = m.get("auction_date")

        if mode == "upcoming":
            records.append(_build_upcoming_row(m, currency, selling_date, scraped_at))
            continue

        sold_status = (m.get("sold_status_label") or "Unsold").strip() or "Unsold"
        selling_price = m.get("selling_price")

        # Sold-only: drop unsold and sold-but-undisclosed (sealed) listings.
        if sold_status != "Sold":
            skipped["unsold"] += 1
            continue
        if selling_price is None:
            skipped["sold_no_price"] += 1
            continue

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
                "Estimate_currency": m.get("estimate_currency"),
                "Sold_status": sold_status,
                "Auction_status": "Completed",
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "FX_rate_used": fx_rate,
                "Selling_date": selling_date,
                "Auction_house": "RM Sotheby's",
                "Country": _country_from_location(m.get("location")),
                "Image_url": m.get("image_url"),
                "Listing_url": m.get("listing_url"),
                "Source": "rmsothebys.com",
                "Listing_description": m.get("description"),
                "Scraped_at": scraped_at,
            },
        )

    for k, v in skipped.items():
        if v:
            logger.info("Skipped %s (%s)", v, k)
    return records


def _build_upcoming_row(m, currency, selling_date, scraped_at):
    """Upcoming lots: no hammer/bid price; estimates are the forward-looking signal."""
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
        "Sold_status": None,
        "Auction_status": "Upcoming",
        "Currency": currency,
        "Selling_price": None,
        "Selling_price_USD": None,
        "FX_rate_used": None,
        "Selling_date": selling_date,
        "Auction_house": "RM Sotheby's",
        "Country": _country_from_location(m.get("location")),
        "Image_url": m.get("image_url"),
        "Listing_url": m.get("listing_url"),
        "Source": "rmsothebys.com",
        "Listing_description": m.get("description"),
        "Scraped_at": scraped_at,
    }
