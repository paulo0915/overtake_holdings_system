"""Hagerty search GraphQL nodes + __NEXT_DATA__ auction page parsing."""

import json
import logging
import re
from typing import Any

from shared.normalizer import (
    extract_description_fields,
    normalize_engine_spec,
    normalize_mileage,
    normalize_transmission,
    unescape,
)

logger = logging.getLogger(__name__)

# Both abbreviations and full names — Hagerty sends full state names ("Virginia", "Ontario").
_CA_PROVINCE_ABBR = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})
_CA_PROVINCE_NAMES = frozenset({
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "northwest territories",
    "nunavut", "ontario", "prince edward island", "quebec", "saskatchewan", "yukon",
})

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def location_to_country(city: str | None, state: str | None) -> str:
    """Infer country from Hagerty city/state.

    Hagerty sends full state names ("Virginia", "Ontario"), not abbreviations.
    Checks both abbreviations and full names to handle either format.
    """
    if not state:
        return "USA"
    st = state.strip()
    if (st.upper() in _CA_PROVINCE_ABBR) or (st.lower() in _CA_PROVINCE_NAMES):
        return "Canada"
    return "USA"


def slug_from_vehicle(year: str | None, make: str | None, model: str | None) -> str:
    """Build URL slug segment: 2023-maserati-mc20 (matches canonical paths)."""

    def seg(s: str | None) -> str:
        t = (s or "").lower().strip()
        t = re.sub(r"[^a-z0-9]+", "-", t)
        return t.strip("-")

    return f"{seg(str(year) if year is not None else '')}-{seg(make)}-{seg(model)}"


def as_year(y: Any) -> int | None:
    """Coerce year value (str or int) to int, returning None if not a valid year."""
    if y is None:
        return None
    if isinstance(y, int):
        return y
    s = str(y).strip()
    return int(s) if s.isdigit() else None


def cents_to_usd(amount: int | None) -> int | None:
    """GraphQL monetary amounts are integer cents (e.g. 18190000 → $181,900)."""
    if amount is None:
        return None
    try:
        return int(amount) // 100
    except (TypeError, ValueError):
        return None


def extract_auction_edges(graphql_response: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (auction_nodes, page_info) from a marketplaceSearchPaginated response."""
    try:
        paginated = (
            graphql_response["data"]["marketplaceSearch"]["search"]["marketplaceExplore"]["paginated"]
        )
    except (KeyError, TypeError):
        return [], {}

    edges = paginated.get("edges") or []
    page_info = paginated.get("pageInfo") or {}
    auctions = []
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("__typename") != "AuctionVehicleSearchNode":
            continue
        auctions.append(node)
    return auctions, page_info


def parse_search_summary(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize list API node fields used for filtering + detail URL construction."""
    loc = node.get("auctionLocation") or {}
    bid = (node.get("currentHighestBid") or {}).get("amount")
    photo = (node.get("auctionPhoto") or {}).get("url")

    return {
        "id": node.get("id"),
        "year": node.get("year"),
        "make": node.get("make"),
        "model": node.get("model"),
        "title": unescape(node.get("auctionTitle") or ""),
        "slug": slug_from_vehicle(node.get("year"), node.get("make"), node.get("model")),
        "high_bid_usd": cents_to_usd(bid),
        "successful_bid_count": node.get("successfulBidCount"),
        "end_date_time": node.get("endDateTime"),
        "has_been_sold": node.get("hasBeenSold"),
        "has_been_sold_after": node.get("hasBeenSoldAfter", False),
        "status": node.get("status"),
        "has_reserve": node.get("hasReserve"),
        "image_url": photo,
        "city": loc.get("city"),
        "state": loc.get("state"),
    }


def _contentful_to_plain(description_json: str | None) -> str | None:
    """Flatten Contentful rich-text JSON (vehicle description) to plain text."""
    if not description_json:
        return None
    try:
        root = json.loads(description_json)
    except json.JSONDecodeError:
        return description_json

    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("nodeType") == "text" and "value" in node:
                parts.append(str(node["value"]))
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(root.get("content"))
    text = " ".join(parts).strip()
    return text or None


def parse_auction_html(html: str | None) -> dict[str, Any] | None:
    """
    Parse embedded __NEXT_DATA__ JSON from an auction detail HTML page.

    Returns merged vehicle + auction fields, or None on failure.
    """
    if not html:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        logger.warning("__NEXT_DATA__ not found in auction HTML")
        return None
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid __NEXT_DATA__ JSON: {e}")
        return None

    outer = payload.get("props", {}).get("pageProps", {})
    inner = outer.get("pageProps", outer)
    auction = inner.get("auction")
    if not isinstance(auction, dict):
        logger.warning("No auction object in __NEXT_DATA__")
        return None

    items = auction.get("auctionItems") or []
    vehicle = items[0] if items else {}

    loc = vehicle.get("location") or {}
    mileage_obj = vehicle.get("mileage") or {}
    raw_unit = (mileage_obj.get("mileageUnit") or "MILES").upper()
    unit = "km" if raw_unit.startswith("K") else "miles"
    mileage_raw = str(mileage_obj.get("mileage")) if mileage_obj.get("mileage") is not None else None
    mileage_mi = normalize_mileage(mileage_raw, unit)

    desc_plain = _contentful_to_plain(vehicle.get("description"))
    desc_fields = extract_description_fields(desc_plain or "")

    engine = normalize_engine_spec(vehicle.get("engine")) or desc_fields["engine"]
    transmission = normalize_transmission(vehicle.get("transmission")) or desc_fields["transmission"]
    ext_color = vehicle.get("exteriorColor") or desc_fields["ext_color"]
    int_color = vehicle.get("interiorColor") or desc_fields["int_color"]

    chb = auction.get("currentHighestBid") or {}
    currency = chb.get("currency") or auction.get("currency") or "USD"
    high_bid_usd = cents_to_usd(chb.get("amount"))

    selling_date = None
    edt = auction.get("endDateTime")
    if isinstance(edt, str) and len(edt) >= 10:
        selling_date = edt[:10]

    lot_number = _lot_from_title(vehicle.get("auctionTitle") or "")

    return {
        "auction_id": auction.get("id"),
        "title": unescape(vehicle.get("auctionTitle") or ""),
        "year": as_year(vehicle.get("year")),
        "make": vehicle.get("make"),
        "model": vehicle.get("model"),
        "vin": vehicle.get("vin"),
        "mileage_mi": mileage_mi,
        "ext_color": unescape(ext_color) if ext_color else None,
        "int_color": unescape(int_color) if int_color else None,
        "engine_spec": engine,
        "transmission": transmission,
        "lot_number": lot_number,
        "listing_description": desc_plain,
        "image_url": (vehicle.get("mainPhoto") or {}).get("url"),
        "country": location_to_country(loc.get("city"), loc.get("state")),
        "currency": currency,
        "high_bid_usd": high_bid_usd,
        "has_been_sold": auction.get("hasBeenSold"),
        "status": auction.get("status"),
        "successful_bid_count": auction.get("successfulBidCount"),
        "selling_date": selling_date,
        "has_reserve": vehicle.get("hasReserve"),
    }


def _lot_from_title(title: str) -> str | None:
    m = re.search(r"\(Lot\s*#?\s*([^)]+)\)", title, re.IGNORECASE)
    return m.group(1).strip() if m else None


def sold_status_from_flags(
    has_been_sold: bool | None,
    has_been_sold_after: bool | None,
    status: str | None,
) -> str:
    """
    Map Hagerty sale flags to the project's canonical Sold_status values.

    has_been_sold      = True:  winning bid accepted during the auction.
    has_been_sold_after = True: sold privately after the auction ended (no live winner).
    Both indicate a completed sale; we store both as "Sold" since the price
    is still recorded.  Withdrawn/Cancelled auctions map to "Withdrawn".
    """
    if status and str(status).upper() in ("CANCELLED", "CANCELED", "WITHDRAWN"):
        return "Withdrawn"
    if has_been_sold is True or has_been_sold_after is True:
        return "Sold"
    return "Unsold"
