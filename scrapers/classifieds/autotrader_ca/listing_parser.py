"""Parse AutoTrader.ca listings (description + vehicle + seller + location)."""

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

logger = logging.getLogger(__name__)


# Descriptions arrive as HTML: ``<br/>``, ``<ul><li>…</li></ul>``, entities.
# Convert to readable plain text while preserving paragraph structure.
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_RE = re.compile(r"</li\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_COLLAPSE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_description(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _BR_RE.sub("\n", raw)
    # Mark list-item ends with a newline so bullet prose doesn't run together.
    text = _LI_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = [_WS_COLLAPSE_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


# Mileage arrives as ``"90 km"`` — parse the integer and convert to miles
# downstream via the shared normalizer (``normalize_mileage`` is km-aware).
_MILEAGE_KM_RE = re.compile(r"([\d,]+)\s*km", re.I)

KM_TO_MI = 0.621371


def _parse_mileage_km_to_mi(raw: str | None) -> int | None:
    if not raw:
        return None
    m = _MILEAGE_KM_RE.search(raw)
    if not m:
        return None
    try:
        km = int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return round(km * KM_TO_MI)


# Engine displacement arrives as ``"6,700 cc"``; convert to ``"6.7-Liter"``
# when we can so it matches the schema's canonical form.
_CC_RE = re.compile(r"([\d,]+)\s*cc", re.I)


def _displacement_string(ccm: str | None, fuel: str | None) -> str | None:
    """``"6,700 cc"`` + ``"Gasoline"`` → ``"6.7-Liter"`` (fuel type is ignored
    here — only the displacement becomes part of ``Engine_spec``).

    Returns ``None`` below 900 cc (scooters / placeholder rows).
    """
    if not ccm:
        return None
    m = _CC_RE.search(str(ccm))
    if not m:
        return None
    try:
        cc = int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if cc < 900:
        return None
    litres = cc / 1000.0
    # Round to 1 decimal if under 10L, else 0 decimals (very rare).
    return f"{litres:.1f}-Liter"


def _compose_title(year: Any, make: Any, model_group: Any, model_version: Any) -> str | None:
    """``<year> <make> <model> <modelVersion>`` with modelGroup preferred
    over ``model`` (which can have stray spaces like ``"F 150"`` vs ``"F-150"``).
    """
    parts: list[str] = []
    if year:
        parts.append(str(year))
    if make:
        parts.append(str(make))
    if model_group:
        parts.append(str(model_group))
    if model_version and model_version not in ("", "None"):
        # Strip dealer ad-copy suffixes like "| 300A | Backup Camera | …"
        mv = str(model_version).strip()
        # The modelVersionInput sometimes carries the full ad blurb separated
        # by ``|`` — keep only the first token which is the real trim.
        first = mv.split("|", 1)[0].strip()
        if first:
            parts.append(first)
    return " ".join(parts).strip() or None


# --- Listing parsing ---------------------------------------------------------

# Price strings arrive as ``"$ 666,694"``; this pulls the bare integer.
_PRICE_NUM_RE = re.compile(r"([\d,]+)")


def _parse_price(formatted: str | None) -> int | None:
    if not formatted:
        return None
    m = _PRICE_NUM_RE.search(formatted)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_listing(listing: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one ``pageProps.listings[i]`` into our intermediate shape.

    Dedup key is the AutoScout24 UUID (``id``) — VIN isn't exposed in the
    feed, so we use ``id`` in place of VIN for uniqueness. The site's public
    listing URL (``url`` field) is the permanent link.

    Returns ``None`` on rows missing id/url/price — any one of those missing
    makes the row unusable for storage or price filtering.
    """
    listing_id = (listing.get("id") or "").strip()
    url = (listing.get("url") or "").strip()
    if not listing_id or not url:
        return None

    price_block = listing.get("price") or {}
    price = _parse_price(price_block.get("priceFormatted"))
    if price is None:
        return None

    vehicle = listing.get("vehicle") or {}
    year = vehicle.get("modelYear")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None

    make = vehicle.get("make") or None
    model_group = vehicle.get("modelGroup") or vehicle.get("model") or None
    trim = vehicle.get("modelVersionInput") or None

    transmission = normalize_transmission(vehicle.get("transmission"))
    mileage_mi = _parse_mileage_km_to_mi(vehicle.get("mileageInKm"))

    # Engine: displacement is structured; flesh it out with prose hints so the
    # output matches the canonical ``"6.7-Liter V12"`` form when possible.
    displacement = _displacement_string(vehicle.get("engineDisplacementInCCM"), vehicle.get("fuel"))
    description_raw = listing.get("description") or ""
    description = _clean_description(description_raw)
    engine_spec = _compose_engine_spec(displacement, description)

    ext_color, int_color = _colors_from_description(description)

    # Location / seller.
    loc = listing.get("location") or {}
    country = "Canada" if (loc.get("countryCode") or "").upper() == "CA" else None
    seller = listing.get("seller") or {}
    dealer_name = (seller.get("companyName") or "").strip() or None

    # Cover image: take the first gallery image stripped of its ``/250x188.webp``
    # size suffix so we store a full-resolution URL.
    image_url = _best_image(listing.get("images") or [])

    title = _compose_title(year, make, model_group, trim)

    return {
        "record_id": f"atca_{listing_id}",
        "listing_id": listing_id,
        "title": title,
        "year": year,
        "make": make,
        "model": model_group,
        "trim": trim,
        "mileage_mi": mileage_mi,
        "ext_color": ext_color,
        "int_color": int_color,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "dealer_name": dealer_name,
        "asking_price_cad": price,
        "country": country,
        "image_url": image_url,
        "listing_url": url,
        "description": description,
    }


def _compose_engine_spec(displacement: str | None, description: str | None) -> str | None:
    """Try hard for a canonical ``"6.7-Liter V12"`` form.

    Strategy:
      1. Pull an engine token from the description (``twin-turbocharged
         6.75-liter V12`` etc.) via the shared extractor — it's already
         normalizer-safe.
      2. Otherwise combine the structured displacement with any cylinder-
         config keyword we can find in the description (``V12``, ``flat-six``).
      3. Otherwise return just the displacement (``"6.7-Liter"``).
    """
    if description:
        inferred = extract_description_fields(description)
        if inferred.get("engine"):
            return inferred["engine"]
        # Best-effort combine: displacement from the feed + a cylinder config
        # hint from the prose (``V12`` / ``flat-six`` / ``inline-six`` …).
        if displacement:
            layout = _extract_layout(description)
            if layout:
                return f"{displacement} {layout}"
    if displacement:
        return normalize_engine_spec(displacement) or displacement
    return None


_LAYOUT_RE = re.compile(
    r"\b(V(?:8|10|12|6)|flat[- ](?:six|eight|four|twelve)|"
    r"inline[- ]?\d|straight[- ]?\d|boxer[- ](?:four|six)|W(?:12|16))\b",
    re.I,
)


def _extract_layout(description: str) -> str | None:
    m = _LAYOUT_RE.search(description)
    return m.group(1) if m else None


def _colors_from_description(description: str | None) -> tuple[str | None, str | None]:
    """AutoTrader.ca doesn't expose structured colours — pull them from the
    description via the shared normalizer (uses context-aware regexes that
    avoid ``"Automatic Headlights"``-style false positives).
    """
    if not description:
        return (None, None)
    inferred = extract_description_fields(description)
    ext = inferred.get("ext_color") or None
    intc = inferred.get("int_color") or None
    return (ext, intc)


def _best_image(images: list[Any]) -> str | None:
    """Return the first gallery image at full resolution.

    The feed serves ``/250x188.webp`` thumbnails; the same path with no size
    suffix gets the original. Strip the size suffix defensively.
    """
    if not images:
        return None
    first = images[0]
    if not isinstance(first, str):
        return None
    # ``…/abc.jpg/250x188.webp`` → ``…/abc.jpg``
    return re.sub(r"/\d+x\d+\.webp$", "", first.strip()) or first.strip()
