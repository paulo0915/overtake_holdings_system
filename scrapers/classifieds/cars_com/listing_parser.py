"""Parse Cars.com GraphQL list rows + VDP detail payloads."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from shared.normalizer import normalize_engine_spec, normalize_transmission

from .config import DETAIL_URL_TEMPLATE, SITE_ORIGIN

logger = logging.getLogger(__name__)

# ``<br>``, ``<br/>``, ``<br />`` all become a newline before the HTML-tag
# strip so paragraph breaks from dealer copy survive.
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_COLLAPSE_RE = re.compile(r"[ \t]+")
_NEWLINE_COLLAPSE_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str | None:
    """Strip HTML from a seller-written ``adDescription`` while preserving
    paragraph structure. Cars.com dealers embed a lot of ``<br>``-separated
    prose so we convert tags → newlines before collapsing whitespace.
    """
    if not raw:
        return None
    # 1. <br> → \n  2. strip remaining tags  3. unescape entities
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    # Tidy whitespace without destroying intentional blank lines.
    lines = [ _WS_COLLAPSE_RE.sub(" ", ln).strip() for ln in text.splitlines() ]
    text = "\n".join(lines)
    text = _NEWLINE_COLLAPSE_RE.sub("\n\n", text).strip()
    return text or None


def _parse_analytics_context(row: dict[str, Any]) -> dict[str, Any]:
    """``analytics.context`` is a stringified JSON blob — parse it safely
    and return ``{}`` on any failure. Sits directly on the ``SrpListingGridCard``,
    not nested under ``listing``.
    """
    raw = ((row.get("analytics") or {}).get("context")) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def parse_list_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a single ``searchResults.results[i]`` into our intermediate shape.

    Most useful fields live inside the stringified ``analytics.context`` JSON.
    Top-level GraphQL fields are often null so we trust ``context`` first.

    Returns ``None`` for rows missing ``listingId`` — no way to fetch detail
    without it.
    """
    listing_id = (row.get("listingId") or "").strip()
    if not listing_id:
        return None

    ctx = _parse_analytics_context(row)

    vin = (ctx.get("vin") or "").strip() or None
    price_raw = ctx.get("price")
    price = None
    if price_raw is not None:
        try:
            price = int(float(price_raw))
        except (TypeError, ValueError):
            price = None

    year_raw = ctx.get("year") or ctx.get("modelYear")
    year: int | None = None
    if year_raw is not None:
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            year = None

    make = (ctx.get("make") or "").strip() or None
    model = (ctx.get("model") or "").strip() or None
    trim = (ctx.get("trim") or "").strip() or None

    mileage_raw = ctx.get("mileage")
    mileage_mi = None
    if mileage_raw is not None:
        try:
            mileage_mi = int(float(mileage_raw))
        except (TypeError, ValueError):
            mileage_mi = None

    title_parts = [str(p) for p in (year, make, model, trim) if p]
    title = " ".join(title_parts).strip() or None

    seller_name = (
        ctx.get("sellerName")
        or ctx.get("dealerName")
        or ctx.get("seller_name")
    )
    if isinstance(seller_name, str):
        seller_name = seller_name.strip() or None

    listing_url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, listing_id=listing_id)

    return {
        "record_id": f"cars_{listing_id}",
        "listing_id": listing_id,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "vin": vin,
        "mileage_mi": mileage_mi,
        "dealer_name": seller_name,
        "asking_price": price,
        "currency": "USD",
        "country": "USA",  # Cars.com is US-only
        "listing_url": listing_url,
    }


def parse_detail_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten ``listingDetails`` response to fields we care about. Missing
    pieces come back as ``None``.
    """
    if not payload:
        return {}

    inventory = payload.get("inventory") or {}
    disp = inventory.get("inventoryDisplay") or {}
    dealer = payload.get("dealer") or {}

    # Dealer: prefer the dealer block; fall back to adDescription's seller line.
    dealer_name = (dealer.get("name") or "").strip() or None

    images = disp.get("imageUrls") or []
    image_url = images[0] if images else None

    ext = (disp.get("exteriorColor") or "").strip() or None
    intc = (disp.get("interiorColor") or "").strip() or None

    engine_raw = (disp.get("engineDescription") or "").strip() or None
    engine_spec = normalize_engine_spec(engine_raw) if engine_raw else None

    trans_raw = (disp.get("transmissionDescription") or "").strip() or None
    transmission = normalize_transmission(trans_raw) if trans_raw else None

    description = html_to_text(disp.get("adDescription"))

    price_raw = inventory.get("listPrice")
    try:
        price = int(float(price_raw)) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    vin = (inventory.get("vin") or "").strip() or None

    mileage_raw = inventory.get("mileage") if inventory.get("mileage") is not None else disp.get("mileage")
    try:
        mileage_mi = int(float(mileage_raw)) if mileage_raw is not None else None
    except (TypeError, ValueError):
        mileage_mi = None

    return {
        "description": description,
        "ext_color": ext,
        "int_color": intc,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "dealer_name": dealer_name,
        "image_url": image_url,
        "vin": vin,
        "asking_price": price,
        "mileage_mi": mileage_mi,
    }
