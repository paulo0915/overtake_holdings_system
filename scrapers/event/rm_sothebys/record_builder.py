"""RM Sotheby's merged rows -> ``AUCTION_COLUMNS``."""

import logging
import re
from typing import Any

from shared.fx_converter import FXConverter

logger = logging.getLogger(__name__)

_US_STATE_RE = re.compile(
    r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    r"Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|"
    r"Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    r"West Virginia|Wisconsin|Wyoming)\b",
    re.I,
)

_COUNTRY_ALIASES = {
    "usa": "USA", "us": "USA", "united states": "USA", "u.s.a.": "USA",
    "uk": "UK", "u.k.": "UK", "united kingdom": "UK", "england": "UK", "scotland": "UK",
}


def _country_from_location(loc: str | None) -> str | None:
    if not loc:
        return None
    s = loc.strip()
    if _US_STATE_RE.search(s):
        return "USA"
    tail = s.rsplit(",", 1)[-1].strip()
    key = tail.lower()
    return _COUNTRY_ALIASES.get(key, tail or None)


async def build_records(
    rows: list[dict[str, Any]],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict[str, Any]]:
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

        currency = (m.get("currency") or "USD").strip() or "USD"
        sold_status = (m.get("sold_status_label") or "Unsold").strip() or "Unsold"
        selling_price = m.get("selling_price")
        bid_price = m.get("bid_price")
        selling_date = m.get("auction_date")

        # Sold-only: drop unsold and sold-but-undisclosed (sealed) listings.
        if sold_status != "Sold":
            skipped["unsold"] += 1
            continue
        if selling_price is None:
            skipped["sold_no_price"] += 1
            continue

        selling_price_usd = bid_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )
        elif bid_price is not None:
            bid_price_usd, fx_rate = await fx_converter.convert_to_usd(
                bid_price, currency, selling_date,
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
                "Currency": currency,
                "Selling_price": selling_price,
                "Selling_price_USD": selling_price_usd,
                "Bid_price": bid_price,
                "Bid_price_USD": bid_price_usd,
                "Bid_count": None,
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
