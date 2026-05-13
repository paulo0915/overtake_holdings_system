"""Parse TrueCar GraphQL list edges + ``__NEXT_DATA__`` detail payloads."""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import normalize_engine_spec, normalize_transmission

from .config import DETAIL_URL_TEMPLATE, SITE_ORIGIN

logger = logging.getLogger(__name__)


# Seller notes come back as a single long string with no real structure;
# dealers typically use double/triple spaces as soft paragraph breaks.
# Collapse runs of whitespace but keep paragraph-ish breaks at 3+ spaces.
_PARAGRAPH_BREAK_RE = re.compile(r" {3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_seller_notes(raw: str | None) -> str | None:
    """Tidy a TrueCar ``sellerNotes`` blob for display/storage.

    Dealer copy on TrueCar has no real formatting — heuristic: treat 3+
    consecutive spaces as a paragraph break and unescape HTML entities.
    """
    if not raw:
        return None
    text = html_lib.unescape(raw)
    text = _PARAGRAPH_BREAK_RE.sub("\n\n", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


# --- List parsing ------------------------------------------------------------

def _compose_title(year: Any, make: Any, model: Any, trim: Any) -> str | None:
    parts = [str(p) for p in (year, make, model, trim) if p]
    return " ".join(parts).strip() or None


def parse_list_edge(edge: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one ``marketplaceListingSearch.edges[i]`` into our shape.

    Returns ``None`` when the edge lacks the essentials (``vin`` + ``id`` +
    ``listPrice``) — without them we can't build a stable ``listing_url``
    or do price filtering.
    """
    node = (edge or {}).get("node") or {}
    if not node:
        return None

    vehicle = node.get("vehicle") or {}
    vin = (vehicle.get("vin") or "").strip() or None
    if not vin:
        return None

    pricing = node.get("pricing") or {}
    # New cars have ``listPrice: null`` and use ``baseMsrp`` / ``totalMsrp``
    # instead — fall through to the first non-null in that order.
    price_raw = (
        pricing.get("listPrice")
        or pricing.get("totalMsrp")
        or pricing.get("baseMsrp")
    )
    try:
        price = int(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    year = vehicle.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    make = (vehicle.get("make") or {}).get("name")
    model = (vehicle.get("model") or {}).get("name")
    style = vehicle.get("style") or {}
    trim = style.get("trimName") or None

    ext = (vehicle.get("exteriorColor") or "").strip() or None
    intc = (vehicle.get("interiorColor") or "").strip() or None

    engine_spec = normalize_engine_spec(vehicle.get("engine"))
    transmission = normalize_transmission(vehicle.get("transmission"))

    mileage_raw = vehicle.get("mileage")
    try:
        mileage_mi = int(mileage_raw) if mileage_raw is not None else None
    except (TypeError, ValueError):
        mileage_mi = None

    dealer = node.get("dealership") or {}
    dealer_name = (dealer.get("name") or "").strip() or None

    # First gallery image is the hero shot.
    image_url = None
    gallery = (node.get("galleryImages") or {}).get("edges") or []
    if gallery:
        first = (gallery[0] or {}).get("node") or {}
        image_url = first.get("url") or None

    listing_url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, vin=vin)
    title = _compose_title(year, make, model, trim)

    return {
        "record_id": f"tc_{vin}",
        "vin": vin,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "trim": trim,
        "mileage_mi": mileage_mi,
        "ext_color": ext,
        "int_color": intc,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "dealer_name": dealer_name,
        "asking_price": price,
        "currency": "USD",
        "country": "USA",  # TrueCar is US-only
        "image_url": image_url,
        "listing_url": listing_url,
        "is_new": (vehicle.get("condition") == "NEW"),
        "is_cpo": bool(vehicle.get("certifiedPreOwned")),
    }


# --- Detail parsing ----------------------------------------------------------

def _find_listing_vehicle(apollo: dict[str, Any]) -> dict[str, Any] | None:
    """Find the Listing.vehicle object in the Apollo store.

    TrueCar normalizes by ref in most stores but inlines ``vehicle`` on
    Listing entries for the VDP bundle — handle both shapes.
    """
    listing_entries = [k for k in apollo.keys() if k.startswith("Listing:")]
    if not listing_entries:
        return None
    listing = apollo.get(listing_entries[0]) or {}
    vehicle = listing.get("vehicle")
    if isinstance(vehicle, dict):
        if vehicle.get("__ref"):
            # Resolve ref one level.
            return apollo.get(vehicle["__ref"]) or None
        return vehicle
    return None


def _seller_notes_from_html(html_text: str) -> str | None:
    """Parse raw VDP HTML for the Seller Notes section.

    TrueCar renders it in a ``<div data-test="vehicle-comments">`` block
    (or similar). Used as fallback when the Apollo state has no sellerNotes.
    """
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "lxml")
    # data-test variants seen on TrueCar VDPs
    for attr_val in ("vehicle-comments", "seller-notes", "sellerNotes", "seller_notes"):
        el = soup.find(attrs={"data-test": attr_val})
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return _clean_seller_notes(text)
    # aria-label fallback
    for label in ("Seller Notes", "Seller's Notes", "Seller Comments"):
        el = soup.find(attrs={"aria-label": re.compile(label, re.I)})
        if el:
            text = el.get_text(" ", strip=True)
            if text:
                return _clean_seller_notes(text)
    # heading-based fallback: find h2/h3 containing "seller" then grab next sibling
    for heading in soup.find_all(["h2", "h3"]):
        if re.search(r"seller", heading.get_text(), re.I):
            container = heading.find_parent()
            if container:
                text = container.get_text(" ", strip=True)
                # Strip the heading itself from the text
                text = re.sub(re.escape(heading.get_text(strip=True)), "", text, count=1).strip()
                if text:
                    return _clean_seller_notes(text)
    return None


def parse_detail_payload(
    root: dict[str, Any] | None,
    html: str | None = None,
) -> dict[str, Any]:
    """Extract seller notes + fallback specs from ``__NEXT_DATA__`` + raw HTML.

    ``sellerNotes`` is on the **Listing** object in Apollo state, NOT on the
    Vehicle object — the previous implementation looked in the wrong place.
    Raw HTML is used as a secondary fallback when the Apollo entry is absent.
    """
    apollo: dict[str, Any] = {}
    if root:
        apollo = (root.get("props") or {}).get("pageProps", {}).get("__APOLLO_STATE__") or {}

    # --- seller notes: Listing object first, then HTML fallback ---------------
    description: str | None = None
    for key, obj in apollo.items():
        if key.startswith("Listing:") and isinstance(obj, dict):
            raw_notes = obj.get("sellerNotes") or obj.get("sellerNote") or obj.get("notes")
            if raw_notes:
                description = _clean_seller_notes(str(raw_notes))
                break

    if not description and html:
        description = _seller_notes_from_html(html)

    # --- vehicle specs --------------------------------------------------------
    vehicle = _find_listing_vehicle(apollo) if apollo else None
    if not vehicle:
        return {"description": description}

    ext = _resolve_color(apollo, vehicle.get("exteriorColor"))
    intc = _resolve_color(apollo, vehicle.get("interiorColor"))
    engine_spec = normalize_engine_spec(vehicle.get("engine"))
    transmission = normalize_transmission(vehicle.get("transmission"))

    mileage_raw = vehicle.get("mileage")
    try:
        mileage_mi = int(mileage_raw) if mileage_raw is not None else None
    except (TypeError, ValueError):
        mileage_mi = None

    return {
        "description": description,
        "ext_color": ext,
        "int_color": intc,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "mileage_mi": mileage_mi,
        "vin": (vehicle.get("vin") or "").strip() or None,
    }


def _resolve_color(apollo: dict[str, Any], color: Any) -> str | None:
    """Resolve ``VehicleColor`` ref (or inline dict) to a display string."""
    if not color:
        return None
    if isinstance(color, str):
        return color.strip() or None
    if isinstance(color, dict):
        if color.get("__ref"):
            resolved = apollo.get(color["__ref"]) or {}
            return (resolved.get("name") or resolved.get("displayName") or "").strip() or None
        return (color.get("name") or color.get("displayName") or "").strip() or None
    return None
