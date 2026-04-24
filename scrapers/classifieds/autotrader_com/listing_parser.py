"""Parse AutoTrader.com REST list rows + VDP HTML.

The JSON shape isn't fully documented anywhere — field names below are what
the live ``/rest/lsc/listing`` endpoint returns as of 2026. If AutoTrader
rotates a field name, this parser is where to patch it.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from shared.normalizer import (
    normalize_engine_spec,
    normalize_transmission,
)

from .config import DETAIL_URL_TEMPLATE, SITE_ORIGIN

logger = logging.getLogger(__name__)


# --- List parsing ------------------------------------------------------------

def _first_str(*candidates: Any) -> str | None:
    """Return the first argument that is a non-empty string (trimmed)."""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _compose_title(row: dict[str, Any]) -> str | None:
    """``<year> <make> <model> <trim>`` from whichever fields are present."""
    year = row.get("year")
    make = _first_str(
        (row.get("make") or {}).get("name") if isinstance(row.get("make"), dict) else row.get("make"),
        row.get("makeName"),
    )
    model = _first_str(
        (row.get("model") or {}).get("name") if isinstance(row.get("model"), dict) else row.get("model"),
        row.get("modelName"),
    )
    trim = _first_str(
        (row.get("trim") or {}).get("name") if isinstance(row.get("trim"), dict) else row.get("trim"),
        row.get("trimName"),
    )
    parts = [str(p) for p in (year, make, model, trim) if p]
    return " ".join(parts).strip() or None


def parse_list_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one listing from ``listings[]`` into our intermediate shape.

    Skips tiles with no listing id OR no price — we need both for dedup and
    the min-price short-circuit. VIN is often present but not guaranteed.
    """
    listing_id = row.get("id") or row.get("listingId")
    if not listing_id:
        return None
    listing_id = str(listing_id)

    # Price: AutoTrader's REST returns price fields in varying shapes. The
    # one most reliably populated is ``pricingDetail.salePrice`` or top-level
    # ``derivedPrice`` / ``price``.
    pricing = row.get("pricingDetail") or row.get("pricing") or {}
    price = _to_int(
        pricing.get("salePrice")
        or pricing.get("primary")
        or row.get("derivedPrice")
        or row.get("price")
    )
    if price is None:
        return None

    vin = _first_str(row.get("vin"), (row.get("vehicle") or {}).get("vin"))

    # Year / make / model / trim — vary between ``vehicle.*`` and top-level.
    vehicle = row.get("vehicle") or {}
    year = _to_int(vehicle.get("year") or row.get("year"))
    row_with_vehicle = {**row, **vehicle}  # shallow merge so _compose_title sees both
    title = _compose_title(row_with_vehicle)
    make = _first_str(
        (vehicle.get("make") or {}).get("name") if isinstance(vehicle.get("make"), dict) else vehicle.get("make"),
        vehicle.get("makeName"),
        (row.get("make") or {}).get("name") if isinstance(row.get("make"), dict) else row.get("make"),
        row.get("makeName"),
    )
    model = _first_str(
        (vehicle.get("model") or {}).get("name") if isinstance(vehicle.get("model"), dict) else vehicle.get("model"),
        vehicle.get("modelName"),
        (row.get("model") or {}).get("name") if isinstance(row.get("model"), dict) else row.get("model"),
        row.get("modelName"),
    )
    trim = _first_str(
        (vehicle.get("trim") or {}).get("name") if isinstance(vehicle.get("trim"), dict) else vehicle.get("trim"),
        vehicle.get("trimName"),
    )

    mileage_mi = _to_int(
        (row.get("specifications") or {}).get("mileage", {}).get("value")
        if isinstance(row.get("specifications"), dict) else None,
    ) or _to_int(row.get("mileage"))

    transmission = normalize_transmission(
        _first_str(
            (row.get("specifications") or {}).get("transmission", {}).get("value")
            if isinstance(row.get("specifications"), dict) else None,
            row.get("transmission"),
        )
    )

    engine_spec = normalize_engine_spec(
        _first_str(
            (row.get("specifications") or {}).get("engine", {}).get("value")
            if isinstance(row.get("specifications"), dict) else None,
            row.get("engineDescription"),
            row.get("engine"),
        )
    )

    ext_color = _first_str(
        (row.get("specifications") or {}).get("color", {}).get("value")
        if isinstance(row.get("specifications"), dict) else None,
        row.get("extColor"),
        row.get("exteriorColor"),
    )
    int_color = _first_str(
        row.get("interiorColor"),
        row.get("intColor"),
    )

    owner = row.get("owner") or {}
    dealer_name = _first_str(
        owner.get("name"),
        (row.get("dealer") or {}).get("name"),
        row.get("dealerName"),
    )

    # Images — ``images.sources[]`` on REST, or ``media.primaryPhoto``.
    image_url = None
    images = row.get("images") or {}
    if isinstance(images, dict):
        sources = images.get("sources") or []
        if sources and isinstance(sources[0], dict):
            image_url = sources[0].get("src")
    if not image_url:
        media = row.get("media") or {}
        image_url = (media.get("primaryPhoto") or {}).get("url") if isinstance(media, dict) else None

    listing_url = DETAIL_URL_TEMPLATE.format(origin=SITE_ORIGIN, listing_id=listing_id)

    is_new = (row.get("type") == "NEW") or (row.get("listingType") == "NEW")

    # The list response carries a ``description.label`` — a truncated
    # version of the dealer blurb (~143 chars, ``hasMore: True`` means
    # the VDP has more). Capture it so ``record_builder`` can use it as
    # a fallback when the VDP extraction returns nothing (e.g. new-car
    # listings where dealers haven't written notes).
    #
    # Some dealers paste raw HTML (``<br>``, ``<p>``) into the blurb,
    # so run it through the same ``_html_to_text`` cleanup the VDP path
    # uses — otherwise those listings end up with ``Listing_description``
    # like ``"<br>"`` which is worse than null.
    desc_block = row.get("description") or {}
    description_short = None
    if isinstance(desc_block, dict):
        label = desc_block.get("label")
        if isinstance(label, str) and label.strip():
            description_short = _html_to_text(label)

    return {
        "record_id": f"atcom_{listing_id}",
        "listing_id": listing_id,
        "vin": vin,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "trim": trim,
        "mileage_mi": mileage_mi,
        "ext_color": ext_color,
        "int_color": int_color,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "dealer_name": dealer_name,
        "asking_price": price,
        "currency": "USD",
        "country": "USA",
        "image_url": image_url,
        "listing_url": listing_url,
        "is_new": is_new,
        "description_short": description_short,
    }


# --- Detail parsing ----------------------------------------------------------

# AutoTrader VDPs ship the full dealer-written text in the Next.js hydration
# blob under:
#   props.pageProps.__eggsState.inventory.<listingId>.fullDescription
# This is the same body the "More from the seller" section renders at
# runtime. We pull it directly from ``__NEXT_DATA__`` instead of reading
# the rendered DOM — faster and doesn't need a live browser to be nav'd
# to the page. The raw VDP HTML already contains the JSON blob server-
# side, so a simple fetch gives us the text.
#
# Fallback fields on some listing variants:
#   ``sellerNotes`` — older inventory schema
#   ``vehicleDescription`` — rarely used
# We look for these in order of specificity, ignoring ``description`` and
# ``safetyRecall.description`` (those are the recall boilerplate).
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.S,
)

# ``fullDescription`` in-line inside __NEXT_DATA__ JSON (escaped string
# form). Used as a faster fallback — pulls the first quoted
# ``"fullDescription":"..."`` without having to JSON-parse the whole
# page state if we already know the key.
_FULL_DESC_RE = re.compile(
    r'"fullDescription"\s*:\s*"((?:\\.|[^"\\])+)"',
    re.S,
)

# Rendered-DOM fallback: the "More from the seller" block shows up as
# ``<div class="more-content collapse"><div><p class="text-left">…</p>``
# after hydration. Only useful when the caller fed us fully-rendered
# HTML (``page.content()`` after navigation), not just the raw HTML.
_MORE_FROM_SELLER_RE = re.compile(
    r'<div[^>]*class="[^"]*more-content[^"]*"[^>]*>.*?<p[^>]*class="[^"]*text-left[^"]*"[^>]*>(.+?)</p>',
    re.S | re.I,
)

_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_COLLAPSE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = [_WS_COLLAPSE_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


def _unescape_json_string(s: str) -> str:
    """Decode ``\\"`` / ``\\n`` / ``\\u0026`` etc. from a raw regex capture."""
    try:
        # Reusing json's string decoder handles all the standard escapes
        # without requiring the surrounding braces.
        return json.loads(f'"{s}"')
    except json.JSONDecodeError:
        # Defensive fallback — replace a couple of common escapes manually.
        return (
            s.replace(r"\"", '"')
            .replace(r"\/", "/")
            .replace(r"\n", "\n")
            .replace(r"\t", "\t")
            .replace(r"\\", "\\")
        )


def _extract_full_description(html: str) -> str | None:
    """Pull ``fullDescription`` from the ``__NEXT_DATA__`` blob.

    Tries two strategies, in order:

    1. A targeted regex for ``"fullDescription":"..."`` — fast, tolerant
       of structural changes (doesn't care what path the field is under).
    2. Full JSON parse of ``__NEXT_DATA__`` + walk looking for the key.
       Used only if (1) misses — pays the ~20-30 ms parse cost.
    """
    # Fast path: regex directly. ``>= 20 chars after cleanup`` is the
    # floor — shorter values are usually stripped-down junk (``<br>``
    # or a 1-word "Reserved" placeholder). Fall through to the slow
    # walk so we try ``sellerComments`` / ``additionalInfo`` next.
    m = _FULL_DESC_RE.search(html)
    if m:
        raw = _unescape_json_string(m.group(1))
        cleaned = _html_to_text(raw)
        if cleaned and len(cleaned.strip()) >= 20:
            return cleaned

    # Slow path: full JSON parse + walk.
    blob_match = _NEXT_DATA_RE.search(html)
    if not blob_match:
        return None
    try:
        data = json.loads(blob_match.group(1))
    except json.JSONDecodeError:
        return None

    def walk(obj: Any, depth: int = 0) -> str | None:
        if depth > 8:
            return None
        if isinstance(obj, dict):
            # Check targeted keys first, in order of specificity.
            # ``additionalInfo`` and ``sellerComments`` show up on some
            # dealer-schema variants where ``fullDescription`` is empty.
            for key in (
                "fullDescription",
                "sellerNotes",
                "sellerComments",
                "dealerComments",
                "additionalInfo",
                "vehicleDescription",
            ):
                val = obj.get(key)
                if isinstance(val, str) and len(val.strip()) > 50:
                    return val
            for v in obj.values():
                found = walk(v, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj[:20]:
                found = walk(item, depth + 1)
                if found:
                    return found
        return None

    raw = walk(data)
    cleaned = _html_to_text(raw) if raw else None
    # Reject stripped-to-nothing cleanups (``<br>`` → ``''`` etc.). We
    # want the caller's short-label fallback to kick in instead of
    # storing empty or whitespace-only text.
    if cleaned and len(cleaned.strip()) >= 20:
        return cleaned
    return None


def parse_detail_html(html: str | None) -> dict[str, Any]:
    """Pull dealer-written description from the VDP HTML.

    The canonical source is ``fullDescription`` in ``__NEXT_DATA__`` —
    this is the text rendered under the "More from the seller" heading
    on the live page. Falls back to a rendered-DOM regex for edge cases
    where the caller supplied ``page.content()`` after hydration.
    """
    if not html:
        return {}

    description = _extract_full_description(html)
    if not description:
        # DOM fallback — only matches in fully-rendered HTML.
        m = _MORE_FROM_SELLER_RE.search(html)
        if m:
            description = _html_to_text(m.group(1))

    return {"description": description}
