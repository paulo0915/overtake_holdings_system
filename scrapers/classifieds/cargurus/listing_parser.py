"""Parse CarGurus list tiles + detail-page Remix loader payloads."""

from __future__ import annotations

import re
from typing import Any

from shared.normalizer import (
    extract_description_fields,
    normalize_engine_spec,
    normalize_transmission,
)

from .config import DETAIL_URL_TEMPLATE, LISTING_TILE_TYPES, SITE_ORIGIN


# CarGurus dealer descriptions wrap structural markers in ``[!@@ ... @@!]``.
# The common case is ``[!@@ NL @@!]`` (newline) — but section headers also
# appear as ``[!@@Additional Info@@!]`` / ``[!@@Features@@!]``. Swap the NL
# variant for real newlines, and surface section headers as ``\n\n<X>\n``
# so downstream readers can see them.
_NL_TOKEN_RE = re.compile(r"\[!@@\s*NL\s*@@!\]")
_SECTION_TOKEN_RE = re.compile(r"\[!@@\s*([^@][^\]]*?)\s*@@!\]")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_description(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _NL_TOKEN_RE.sub("\n", raw)
    # Section headers: ``[!@@Additional Info@@!]`` → ``\n\nAdditional Info\n``.
    text = _SECTION_TOKEN_RE.sub(lambda m: f"\n\n{m.group(1).strip()}\n", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


def is_listing_tile(tile: dict[str, Any]) -> bool:
    """True for real car-listing tiles, False for ads / merch cards."""
    return tile.get("type") in LISTING_TILE_TYPES


def parse_list_tile(tile: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a ``LISTING_USED_STANDARD`` / ``LISTING_NEW_STANDARD`` tile.

    Returns ``None`` if the tile is missing fields needed to build a stable
    ``listing_url`` — without ``id`` we can't fetch detail or dedupe.
    """
    if not is_listing_tile(tile):
        return None

    d = tile.get("data") or {}
    lid = d.get("id")
    if lid is None:
        return None
    listing_id = str(lid)

    ont = d.get("ontologyData") or {}
    year = None
    if ont.get("carYear"):
        try:
            year = int(ont["carYear"])
        except (TypeError, ValueError):
            year = None

    price_raw = (d.get("priceData") or {}).get("current")
    try:
        price = int(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    mileage_raw = (d.get("mileageData") or {}).get("value")
    try:
        mileage_mi = int(mileage_raw) if mileage_raw is not None else None
    except (TypeError, ValueError):
        mileage_mi = None

    seller = d.get("sellerData") or {}
    # ``serviceProviderName`` is the public dealer brand (e.g. "Ferrari
    # Maserati of Palm Beach"); ``franchiseMake`` is the OEM they represent —
    # the brand is what we want as ``Dealer_name``.
    dealer_name = (seller.get("serviceProviderName") or "").strip() or None

    ext = ((d.get("exteriorColorData") or {}).get("localized") or "").strip() or None
    intc = ((d.get("interiorColorData") or {}).get("localized") or "").strip() or None

    engine_spec = normalize_engine_spec(d.get("localizedEngineName"))
    transmission = normalize_transmission(d.get("localizedTransmission"))

    image_url = (d.get("pictureData") or {}).get("url") or None

    listing_url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, listing_id=listing_id)

    return {
        "record_id": f"cg_{listing_id}",
        "listing_id": listing_id,
        "listing_title": d.get("listingTitle"),
        "year": year,
        "make": ont.get("makeName"),
        "model": ont.get("modelName"),
        "trim": ont.get("trimName"),
        "vin": (d.get("vin") or "").strip() or None,
        "mileage_mi": mileage_mi,
        "ext_color": ext,
        "int_color": intc,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "dealer_name": dealer_name,
        "asking_price": price,
        "currency": "USD",
        "country": "USA",
        "image_url": image_url,
        "listing_url": listing_url,
        "is_new": bool(d.get("isNew")),
        "is_cpo": bool(d.get("isCpo")),
    }


def parse_detail_payload(root: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the detail-page loader JSON to fields we actually store.

    The canonical fields we keep:
        description, ext_color, int_color, engine_spec, transmission,
        dealer_name, image_url, vin, asking_price, mileage_mi, msrp.

    Most are duplicated from the list tile — but the list's colors are
    already high-quality so detail is really here for ``description`` (the
    long dealer-written blob). We still pick detail values when non-empty,
    with the list as fallback handled by ``record_builder``.
    """
    if not root:
        return {}

    listing = (root.get("oldData") or {}).get("listing") or {}
    if not listing:
        # ``oldData`` is sometimes absent; try ``data.listing`` as a fallback.
        listing = (root.get("data") or {}).get("listing") or {}
    if not listing:
        return {}

    description = _clean_description(listing.get("description"))

    ext = (listing.get("localizedExteriorColor") or "").strip() or None
    intc = (listing.get("localizedInteriorColor") or "").strip() or None

    engine_spec = normalize_engine_spec(listing.get("localizedEngineDisplayName"))
    transmission = normalize_transmission(listing.get("localizedTransmission"))

    # Last-resort prose fallback: brand-new pre-delivery inventory often has
    # no structured transmission field. The shared extractor uses tight
    # context-aware patterns (``X-speed Y``, ``<type> transmission``) to
    # avoid false positives like "Automatic Headlights" or "Owners Manual".
    if not transmission and description:
        inferred = extract_description_fields(description)
        if inferred.get("transmission"):
            transmission = inferred["transmission"]
        if not engine_spec and inferred.get("engine"):
            engine_spec = inferred["engine"]

    # Pictures carry ``scaledPictures`` with size variants; prefer 1024x768.
    image_url = None
    pics = listing.get("pictures") or []
    if pics:
        first = pics[0] or {}
        image_url = (
            ((first.get("scaledPictures") or {}).get("SIZE_1024x768") or {}).get("url")
            or first.get("url")
        )

    seller = (root.get("data") or {}).get("seller") or {}
    dealer_name = (
        seller.get("name")
        or seller.get("serviceProviderName")
        or seller.get("displayName")
    )
    if isinstance(dealer_name, str):
        dealer_name = dealer_name.strip() or None

    price_raw = listing.get("price")
    try:
        price = int(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    msrp_raw = listing.get("msrp")
    try:
        msrp = int(msrp_raw) if msrp_raw is not None else None
    except (TypeError, ValueError):
        msrp = None

    mileage_raw = listing.get("mileage")
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
        "dealer_name": dealer_name,
        "image_url": image_url,
        "vin": (listing.get("vin") or "").strip() or None,
        "asking_price": price,
        "msrp": msrp,
        "mileage_mi": mileage_mi,
    }
