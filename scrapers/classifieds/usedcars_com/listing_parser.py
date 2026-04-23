"""Parse UsedCars.com Dealix list + detail payloads."""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any

from shared.normalizer import (
    extract_description_fields,
    normalize_engine_spec,
    normalize_transmission,
)

from .config import DETAIL_URL_TEMPLATE, SITE_ORIGIN, TRANSMISSION_CODE_MAP

logger = logging.getLogger(__name__)


# Dealer ``state`` values come back as ``"CA  "`` (padded to 4 chars) — trim.
def _clean_state(s: Any) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    return s or None


def _decode_transmission(raw: str | None) -> str | None:
    """``"A"``/``"M"`` codes → display values via our lookup, falling
    through to the shared normalizer for anything unexpected.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw in TRANSMISSION_CODE_MAP:
        return TRANSMISSION_CODE_MAP[raw]
    return normalize_transmission(raw)


def _compose_title(row: dict[str, Any]) -> str | None:
    parts = [str(p) for p in (
        row.get("year"), row.get("make"), row.get("model"), row.get("style"),
    ) if p]
    return " ".join(parts).strip() or None


def _build_dealer_lookup(dealers: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    """``{dealerId → dealer row}`` for fast join in the main loop."""
    return {d["id"]: d for d in (dealers or []) if "id" in d}


# Seller notes arrive as HTML with ``<br />`` paragraph breaks; convert to
# plain text but keep paragraph structure.
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_COLLAPSE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_seller_notes(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = [_WS_COLLAPSE_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


# The ``addlFeatures`` blob uses ``^`` as a separator. Useful for a short
# feature summary but too noisy to store wholesale in description.
def _parse_features(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split("^") if p.strip()]


# --- List parsing ------------------------------------------------------------

def parse_list_row(
    row: dict[str, Any],
    dealer_lookup: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Flatten one ``carsForSale[i]`` entry joined with its dealer.

    Returns ``None`` for rows missing ``id`` + ``vin`` — we need both to
    dedup and to build a stable listing URL.
    """
    used_car_id = row.get("id")
    vin = (row.get("vin") or "").strip() or None
    if used_car_id is None or not vin:
        return None

    price_raw = row.get("price")
    try:
        price = int(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    year = row.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    mileage_raw = row.get("mileage")
    try:
        mileage_mi = int(mileage_raw) if mileage_raw is not None else None
    except (TypeError, ValueError):
        mileage_mi = None

    transmission = _decode_transmission(row.get("transmission"))

    dealer = dealer_lookup.get(row.get("dealerId")) or {}
    dealer_name = (dealer.get("name") or "").strip() or None

    listing_url = DETAIL_URL_TEMPLATE.format(
        origin=SITE_ORIGIN, used_car_id=used_car_id, vin=vin,
    )

    return {
        "record_id": f"uc_{used_car_id}",
        "used_car_id": used_car_id,
        "vin": vin,
        "title": _compose_title(row),
        "year": year,
        "make": row.get("make"),
        "model": row.get("model"),
        "style": row.get("style"),
        "mileage_mi": mileage_mi,
        "ext_color": (row.get("exteriorColor") or "").strip() or None,
        "int_color": (row.get("interiorColor") or "").strip() or None,
        "transmission": transmission,
        "horse_power": row.get("horsePower"),
        "cylinders": row.get("cylinders"),
        "fuel_type": row.get("fuelType"),
        "dealer_name": dealer_name,
        "dealer_city": (dealer.get("city") or "").strip() or None,
        "dealer_state": _clean_state(dealer.get("state")),
        "asking_price": price,
        "currency": "USD",
        "country": "USA",  # UsedCars.com is US-only
        "image_url": (row.get("imageUrl") or "").strip() or None,
        "listing_url": listing_url,
    }


# --- Detail parsing ----------------------------------------------------------

def parse_detail_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the Dealix detail payload to the fields we actually store.

    The list tile already has almost everything we care about — the detail
    call's real value is ``sellerNotes`` (dealer-written prose). We also
    pull in the richer dealer fields (``dealerName``, ``city``, ``state``,
    ``zip``) and take another shot at engine/transmission in case the
    list-side values were empty.
    """
    if not payload:
        return {}

    description = _clean_seller_notes(payload.get("sellerNotes"))

    transmission = _decode_transmission(payload.get("transmission"))

    # Engine spec best-effort: Dealix doesn't store displacement directly,
    # but ``horsePower`` + ``cylinders`` + ``fuelType`` + the seller blurb
    # together let us reconstruct it. Run the shared extractor over the
    # description first — when a dealer writes "twin-turbocharged flat-six"
    # that's the canonical form. Fall back to a short synthesized string.
    engine_spec = None
    if description:
        inferred = extract_description_fields(description)
        engine_spec = inferred.get("engine")
    if not engine_spec:
        engine_spec = _synthesize_engine_spec(
            horse_power=payload.get("horsePower"),
            cylinders=payload.get("cylinders"),
            fuel_type=payload.get("fuelType"),
        )

    dealer_name = (payload.get("dealerName") or "").strip() or None

    return {
        "description": description,
        "transmission": transmission,
        "engine_spec": engine_spec,
        "dealer_name": dealer_name,
        "dealer_city": (payload.get("city") or "").strip() or None,
        "dealer_state": _clean_state(payload.get("state")),
        "ext_color": (payload.get("exteriorColor") or "").strip() or None,
        "int_color": (payload.get("interiorColor") or "").strip() or None,
        "image_url": (payload.get("imageUrl") or "").strip() or None,
    }


def _synthesize_engine_spec(
    *, horse_power: str | None, cylinders: int | None, fuel_type: str | None,
) -> str | None:
    """Best-effort ``"443 hp 6-cylinder Gas"`` style when we have no
    displacement but still want *something* useful in ``Engine_spec``.

    Returns ``None`` if there's not enough to build a meaningful string —
    rejecting low-signal values (electric with no HP, missing cylinder
    count, etc.) is better than a column full of ``"0 cylinder"``.
    """
    hp = (horse_power or "").strip()
    cyl = cylinders if isinstance(cylinders, int) and cylinders > 0 else None
    fuel_raw = (fuel_type or "").strip()
    # "Gas (Leaded/Unleaded)" → "Gas"; anything else passes through.
    fuel = fuel_raw.split(" (", 1)[0] if fuel_raw else None

    if not (hp or cyl or fuel):
        return None
    parts: list[str] = []
    if hp:
        parts.append(hp)
    if cyl:
        parts.append(f"{cyl}-cylinder")
    if fuel:
        parts.append(fuel)
    synthesized = " ".join(parts).strip() or None
    # Run through the shared normalizer's junk guard so we don't store
    # things like bare "6-cylinder" as an engine spec.
    return normalize_engine_spec(synthesized) or synthesized
