"""BaT record assembly — sold-text parsing, FX conversion, schema mapping."""

import html as html_lib
import logging
import re

from shared.fx_converter import FXConverter
from shared.normalizer import (
    country_code_to_name,
    format_selling_date,
    normalize_engine_spec,
    normalize_mileage,
    normalize_transmission,
    parse_make_model_from_title,
    parse_year_from_title,
    unescape,
)

logger = logging.getLogger(__name__)


def parse_sold_text(sold_text: str | None) -> tuple[str, int | None, int | None, str | None]:
    """Parse BaT sold_text. Returns (status, selling_price, bid_price, date_str)."""
    if not sold_text:
        return ("Withdrawn", None, None, None)

    sold_text = sold_text.strip()

    if sold_text.lower().startswith("sold"):
        status = "Sold"
    elif sold_text.lower().startswith("bid to"):
        status = "Unsold"
    elif "withdrawn" in sold_text.lower():
        status = "Withdrawn"
    else:
        status = "Unsold"

    price_match = re.search(r'(?:[\$€£]|CHF)\s*([\d,]+)', sold_text)
    price = int(price_match.group(1).replace(",", "")) if price_match else None

    date_match = re.search(r'on\s+(\d{1,2})/(\d{1,2})/(\d{4})', sold_text)
    date_str = None
    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = int(date_match.group(3))
        date_str = f"{year:04d}-{month:02d}-{day:02d}"

    selling_price = price if status == "Sold" else None
    bid_price = price if status == "Unsold" else None
    return (status, selling_price, bid_price, date_str)


async def build_records(
    listings: list[dict],
    details: dict[str, dict],
    fx_converter: FXConverter,
    vehicle_filter,
    scraped_at: str,
) -> list[dict]:
    """Assemble final auction records from BaT raw API items and parsed listing details."""
    records = []
    seen_urls: set[str] = set()
    skipped = {"parts": 0, "invalid": 0, "filter": 0, "dup": 0}

    for item in listings:
        url = item.get("url", "")
        if url in seen_urls:
            skipped["dup"] += 1
            continue
        seen_urls.add(url)

        title = html_lib.unescape(item.get("title", ""))
        detail = details.get(url, {})

        if not detail.get("is_valid_listing", True):
            skipped["invalid"] += 1
            continue
        if detail.get("is_parts", False):
            skipped["parts"] += 1
            continue

        # Make & Model
        make = detail.get("make") or None
        model = detail.get("model") or None
        if not make:
            make, model = parse_make_model_from_title(title)
            make = make or None
            model = model or None

        # Vehicle filter — applied regardless of whether make was resolved
        if vehicle_filter:
            if not vehicle_filter.match(title, ""):
                skipped["filter"] += 1
                continue

        # Sold text
        sold_text = item.get("sold_text", "")
        sold_status, selling_price, bid_price, selling_date_str = parse_sold_text(sold_text)
        selling_date = format_selling_date(item.get("timestamp_end"), selling_date_str)

        year = detail.get("year") or parse_year_from_title(title)
        mileage = normalize_mileage(detail.get("mileage_raw"), detail.get("mileage_unit"))
        transmission = normalize_transmission(detail.get("transmission_raw"))
        engine = normalize_engine_spec(detail.get("engine_spec"))

        # FX conversion
        currency = item.get("currency", "USD")
        price_usd, fx_rate = None, None
        bid_price_usd = None

        if selling_price is not None:
            price_usd, fx_rate = await fx_converter.convert_to_usd(selling_price, currency, selling_date)
            if price_usd is None and currency != "USD":
                logger.warning(
                    f"FX conversion failed: {title[:50]!r} ({currency}→USD, date={selling_date})"
                )

        if bid_price is not None:
            bid_price_usd, bid_fx = await fx_converter.convert_to_usd(bid_price, currency, selling_date)
            if bid_price_usd is None and currency != "USD":
                logger.warning(
                    f"FX conversion failed: {title[:50]!r} ({currency}→USD, date={selling_date})"
                )
            if fx_rate is None:
                fx_rate = bid_fx

        country = country_code_to_name(item.get("country_code"))
        image_url = item.get("thumbnail_url")
        if image_url and "?resize=" in image_url:
            image_url = image_url.split("?resize=")[0]

        bat_id = item.get("id")

        records.append({
            "Record_ID": str(bat_id) if bat_id else url,
            "Title": title,
            "Year": year,
            "Make": make,
            "Model": model,
            "VIN": detail.get("vin"),
            "Mileage_mi": mileage,
            "Ext_color": unescape(detail.get("ext_color")),
            "Int_color": unescape(detail.get("int_color")),
            "Engine_spec": engine,
            "Transmission": transmission,
            "Lot_number": detail.get("lot_number"),
            "Estimate_low": None,
            "Estimate_high": None,
            "Estimate_currency": None,
            "Sold_status": sold_status,
            "Auction_status": "Completed",
            "Currency": currency,
            "Selling_price": selling_price,
            "Selling_price_USD": price_usd,
            "Bid_price": bid_price,
            "Bid_price_USD": bid_price_usd,
            "Bid_count": detail.get("bid_count"),
            "FX_rate_used": fx_rate,
            "Selling_date": selling_date,
            "Auction_house": "Bring a Trailer",
            "Country": country,
            "Image_url": image_url,
            "Listing_url": url,
            "Source": "bringatrailer.com",
            "Listing_description": detail.get("listing_description"),
            "Scraped_at": scraped_at,
        })

    for k, v in skipped.items():
        if v:
            logger.info(f"Skipped {v} ({k})")
    return records
