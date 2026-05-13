"""Hagerty — merge list summary + detail parse, FX, AUCTION_COLUMNS mapping."""

import logging
import re

from shared.fx_converter import FXConverter
from shared.normalizer import is_non_car_make

# Titles containing these phrases are parts/engines/memorabilia — not real cars.
_NON_CAR_TITLE_RE = re.compile(
    r"\b(engine(\s+lot)?|gearbox|transaxle|transmission\s+lot|parts?\s+lot"
    r"|pedal\s+car|toy\s+car|scale\s+model|automobilia|memorabilia"
    r"|bodywork|chassis\s+only|rolling\s+chassis|bare\s+chassis"
    r"|wheels?\s+set|wheel\s+lot|tire\s+lot)\b",
    re.IGNORECASE,
)

from .config import HAGERTY_MARKETPLACE_BASE
from .listing_parser import as_year, location_to_country, sold_status_from_flags

logger = logging.getLogger(__name__)


async def build_records(
    summaries: list[dict],
    details: dict[str, dict | None],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict]:
    """Build final auction rows from search summaries + parsed detail dicts."""
    records: list[dict] = []
    seen: set[str] = set()
    skipped = {"filter": 0, "dup": 0}

    for s in summaries:
        aid = str(s.get("id") or "")
        if not aid:
            continue
        if aid in seen:
            skipped["dup"] += 1
            continue
        seen.add(aid)

        title = s.get("title") or ""
        # Drop non-vehicle listings: Hagerty uses year 0 / "0000" as a
        # placeholder for engines / parts / lots with no real model year.
        parsed_year = as_year(s.get("year"))
        if parsed_year is None or parsed_year < 1886:
            skipped["filter"] += 1
            continue
        if is_non_car_make(s.get("make")):
            skipped["filter"] += 1
            continue
        if _NON_CAR_TITLE_RE.search(title):
            skipped["filter"] += 1
            continue
        if vehicle_filter and title:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        d = details.get(aid)

        # ── Price: always use the SEARCH SUMMARY amount ───────────────────────
        # The detail page's currentHighestBid is the pre-premium hammer price.
        # The search API's currentHighestBid is the final buyer-paid total
        # (hammer + buyer's premium, typically 7%).  The search figure is the
        # correct "price" to store for data consistency with other scrapers.
        high_bid_usd = s.get("high_bid_usd")

        # ── All other fields: prefer detail, fall back to summary ─────────────
        if d:
            merged_title = d.get("title") or title
            year = d.get("year") if d.get("year") is not None else as_year(s.get("year"))
            make = d.get("make") or s.get("make")
            model = d.get("model") or s.get("model")
            vin = d.get("vin")
            mileage_mi = d.get("mileage_mi")
            ext_color = d.get("ext_color")
            int_color = d.get("int_color")
            engine_spec = d.get("engine_spec")
            transmission = d.get("transmission")
            lot_number = d.get("lot_number")
            listing_description = d.get("listing_description")
            image_url = d.get("image_url") or s.get("image_url")
            country = d.get("country") or location_to_country(s.get("city"), s.get("state"))
            currency = d.get("currency") or "USD"
            has_been_sold = d.get("has_been_sold") if d.get("has_been_sold") is not None else s.get("has_been_sold")
            has_been_sold_after = s.get("has_been_sold_after", False)
            status = d.get("status") or s.get("status")
            selling_date = d.get("selling_date")
        else:
            merged_title = title
            year = as_year(s.get("year"))
            make = s.get("make")
            model = s.get("model")
            vin = None
            mileage_mi = None
            ext_color = int_color = engine_spec = transmission = lot_number = listing_description = None
            image_url = s.get("image_url")
            country = location_to_country(s.get("city"), s.get("state"))
            currency = "USD"
            has_been_sold = s.get("has_been_sold")
            has_been_sold_after = s.get("has_been_sold_after", False)
            status = s.get("status")
            selling_date = None
            if s.get("end_date_time") and isinstance(s["end_date_time"], str) and len(s["end_date_time"]) >= 10:
                selling_date = s["end_date_time"][:10]

        # hasBeenSoldAfter=True means the car sold privately AFTER the auction
        # ended without a live bid winning. Treat as Sold but flag is noted.
        sold_status = sold_status_from_flags(has_been_sold, has_been_sold_after, status)
        selling_price = high_bid_usd if sold_status == "Sold" else None

        slug = s.get("slug") or ""
        listing_url = f"{HAGERTY_MARKETPLACE_BASE}/auction/{slug}/{aid}"

        selling_price_usd = fx_rate = None
        if selling_price is not None:
            selling_price_usd, fx_rate = await fx_converter.convert_to_usd(
                selling_price, currency, selling_date,
            )
            if selling_price_usd is None and currency != "USD":
                logger.warning(
                    f"FX conversion failed: {merged_title[:50]!r} ({currency}→USD, date={selling_date})"
                )

        records.append({
            "Record_ID": aid,
            "Title": merged_title,
            "Year": year,
            "Make": make,
            "Model": model,
            "VIN": vin,
            "Mileage_mi": mileage_mi,
            "Ext_color": ext_color,
            "Int_color": int_color,
            "Engine_spec": engine_spec,
            "Transmission": transmission,
            "Lot_number": lot_number,
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
            "Auction_house": "Hagerty Marketplace",
                "Auction_event": None,
            "Country": country,
            "Image_url": image_url,
            "Listing_url": listing_url,
            "Source": "hagerty.com",
            "Listing_description": listing_description,
            "Scraped_at": scraped_at,
        })

    for k, v in skipped.items():
        if v:
            logger.info(f"Skipped {v} ({k})")
    return records
