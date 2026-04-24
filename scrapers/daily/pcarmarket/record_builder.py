"""PCarMarket record assembly — lot filtering, FX conversion, schema mapping."""

import logging
import re

from shared.fx_converter import FXConverter

from . import listing_parser as _parser

logger = logging.getLogger(__name__)

# PCarMarket internal listing ID pattern — not a real auction lot number.
# e.g. "V-0039563-MP", "V-0009703", "V-0009703-MP"
_PCAR_INTERNAL_ID_RE = re.compile(r"^V-\d+", re.IGNORECASE)


async def build_records(
    listings: list[dict],
    details: dict[str, dict],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict]:
    """
    Assemble final AUCTION_COLUMNS records from parsed PCarMarket data.

    Applies: dedup → vehicle filter → lot-number validation → FX conversion → schema mapping.
    """
    records = []
    seen_ids: set[int] = set()
    skipped = {"filter": 0, "dup": 0}

    for item in listings:
        item_id = item.get("id")
        if item_id in seen_ids:
            skipped["dup"] += 1
            continue
        seen_ids.add(item_id)

        # Skip members-only previews — PCarMarket lists some cars twice,
        # once on the public auction path and once as ``MP: <title>``
        # (members-only preview) on ``/mp/…``. The MP duplicate has the
        # same VIN as the public one, so keeping it would double-count
        # every high-end listing. Per P1 §1.4.
        raw_title = (item.get("title") or "").strip()
        if raw_title.startswith("MP:"):
            skipped["dup"] += 1
            continue

        # Vehicle filter before any expensive parsing or FX calls
        if vehicle_filter and item.get("title"):
            if not vehicle_filter.match(item["title"], ""):
                skipped["filter"] += 1
                continue

        slug = item.get("slug", "")
        detail = details.get(slug, {})
        parsed = _parser.parse(item, detail)

        # Lot number — reject internal PCarMarket listing IDs
        raw_lot = parsed["lot_number_raw"]
        lot_number = None if (raw_lot and _PCAR_INTERNAL_ID_RE.match(raw_lot)) else raw_lot

        # FX conversion
        selling_price = parsed["selling_price"]
        currency = parsed["currency"]
        selling_date = parsed["selling_date"]
        title = parsed["title"]

        selling_price_usd = fx_rate = None

        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )
            if selling_price_usd is None and currency != "USD":
                logger.warning(
                    f"FX conversion failed: {title[:50]!r} ({currency}→USD, date={selling_date})"
                )


        records.append({
            "Record_ID": str(parsed["item_id"]),
            "Title": title,
            "Year": parsed["year"],
            "Make": parsed["make"],
            "Model": parsed["model"],
            "VIN": parsed["vin"],
            "Mileage_mi": parsed["mileage"],
            "Ext_color": parsed["ext_color"],
            "Int_color": parsed["int_color"],
            "Engine_spec": parsed["engine"],
            "Transmission": parsed["transmission"],
            "Lot_number": lot_number,
            "Estimate_low": None,
            "Estimate_high": None,
            "Estimate_currency": None,
            "Sold_status": parsed["sold_status"],
            "Auction_status": "Completed",
            "Currency": currency,
            "Selling_price": selling_price,
            "Selling_price_USD": selling_price_usd,
            "FX_rate_used": fx_rate,
            "Selling_date": selling_date,
            "Auction_house": "PCarMarket",
            "Country": parsed["country"],
            "Image_url": parsed["image_url"],
            "Listing_url": parsed["listing_url"],
            "Source": "pcarmarket.com",
            "Listing_description": parsed["listing_description"],
            "Scraped_at": scraped_at,
        })

    for k, v in skipped.items():
        if v:
            logger.info(f"Skipped {v} ({k})")
    return records
