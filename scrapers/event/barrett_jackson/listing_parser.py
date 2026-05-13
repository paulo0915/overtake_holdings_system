"""Normalize Barrett-Jackson ``previous-docket-results`` ``data[]`` rows."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from shared.normalizer import normalize_engine_spec, normalize_transmission, normalize_vin

from scrapers.event.mecum import (
    extract_transmission_from_description,
    extract_vin_from_description,
)

from .config import DEFAULT_RESULT_TYPE, SITE_ORIGIN


def _title_case_phrase(s: str | None) -> str | None:
    if not s or not str(s).strip():
        return None
    return str(s).strip().title()


def _selling_date(attrs: dict[str, Any]) -> str | None:
    raw = (attrs.get("run_datetime") or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt.date().isoformat()
    except ValueError:
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            return raw[:10]
    return None


def _mileage_from_narrative(*chunks: str | None) -> int | None:
    """Best-effort mileage from BJ short + full descriptions."""
    text = " ".join(c for c in chunks if c and str(c).strip())
    if not text:
        return None
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    # "1,573-actual-mile" / "573-actual-mile" style
    m = re.search(
        r"\b(\d{1,7})\s*-\s*actual\s*-\s*miles?\b",
        t, re.I,
    )
    if m:
        return int(m.group(1))
    # "just over 1,573 actual miles"
    m = re.search(
        r"\b(?:just\s+over|over|nearly|almost|about|approximately|approx\.?)\s+(\d{1,7})\s+actual\s+miles?\b",
        t, re.I,
    )
    if m:
        return int(m.group(1))
    m = re.search(
        r"(?:^|\s)(\d{1,7})\s+(?:original|actual|indicated|documented|verified|showing)\s+(?:mi\.?|miles?|mile)\b",
        t, re.I,
    )
    if m:
        return int(m.group(1))
    # Plain "N miles" — require at least 3 digits to avoid "1 mile", "5 miles", HP figures.
    m = re.search(r"(?<!\d)(\d{3,7})\s*(?:mi\.?|miles?|mile)\b", t, re.I)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(?<!\d)(\d{3,7})\s*(?:km|kilometers?|kilometres?)\b", t, re.I)
    if m2:
        return round(int(m2.group(1)) * 0.621371)
    return None


def _year(attrs: dict[str, Any]) -> int | None:
    yn = attrs.get("year_numeric")
    if isinstance(yn, int) and 1885 <= yn <= 2035:
        return yn
    y = attrs.get("year")
    if isinstance(y, int) and 1885 <= y <= 2035:
        return y
    if isinstance(y, str) and y.strip().isdigit():
        yi = int(y.strip())
        return yi if 1885 <= yi <= 2035 else None
    return None


def _model_line(attrs: dict[str, Any]) -> str | None:
    model = (attrs.get("model") or "").strip()
    style = (attrs.get("style") or "").strip()
    if model and style:
        return f"{model} {style}".strip()
    return model or style or None


def _engine_spec(attrs: dict[str, Any]) -> str | None:
    eng = (attrs.get("engine_size") or "").strip()   # e.g. "4,593CC", "3.5L", "3.5 L"
    cyl = (attrs.get("number_of_cylinders") or "").strip()  # e.g. "8", "V6", "6"

    # Try engine size alone first — normalizer may already produce a clean result.
    if eng:
        normed = normalize_engine_spec(eng)
        if normed:
            return normed

    # Build a composite string only when needed (e.g. displacement unknown but cylinders known).
    # Prefix a bare digit cylinder count with "V" so it reads as a configuration, not a number.
    cyl_s = None
    if cyl:
        cyl_upper = cyl.upper()
        if cyl_upper.isdigit():
            cyl_s = f"V{cyl_upper}" if int(cyl_upper) in (4, 6, 8, 10, 12, 16) else cyl_upper
        else:
            cyl_s = cyl_upper

    parts = [p for p in (eng, cyl_s) if p]
    if not parts:
        return None
    raw = " ".join(parts)
    return normalize_engine_spec(raw) or raw


def _listing_url(event_slug: str, slug: str) -> str | None:
    """Canonical vehicle page: ``/{event}/docket/vehicle/{slug}?origin=results``."""
    slug = (slug or "").strip()
    if not slug:
        return None
    ev = (event_slug or "").strip().strip("/")
    if ev:
        return f"{SITE_ORIGIN}/{ev}/docket/vehicle/{slug}?origin=results"
    return f"{SITE_ORIGIN}/results?type={quote(DEFAULT_RESULT_TYPE)}&slug={quote(slug)}"


def parse_docket_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one ``{ id, attributes }`` element from ``data``."""
    if not isinstance(entry, dict):
        return None
    attrs = entry.get("attributes")
    if not isinstance(attrs, dict):
        return None
    if attrs.get("is_canceled"):
        return None

    lot_id = (attrs.get("lot_id") or "").strip()
    if not lot_id:
        return None

    slug = (attrs.get("slug") or "").strip()
    title = (attrs.get("title") or "").strip() or None
    short_d = (attrs.get("short_description") or "").strip()
    full_d = (attrs.get("full_description") or "").strip()
    desc_parts = [p for p in (short_d, full_d) if p]
    listing_description = "\n\n".join(desc_parts) if desc_parts else None

    event_slug = (attrs.get("event_slug") or "").strip()
    run_date = (attrs.get("run_date") or "").strip()
    auction_label = None
    if event_slug:
        auction_label = event_slug.replace("-", " ").title()
    elif run_date:
        auction_label = run_date

    is_sold = bool(attrs.get("is_sold"))
    price_dec = attrs.get("price_decimal")
    hammer: int | None = None
    if isinstance(price_dec, int):
        hammer = price_dec
    elif isinstance(price_dec, float) and price_dec.is_integer():
        hammer = int(price_dec)

    vin = normalize_vin((attrs.get("vin") or attrs.get("VIN") or "").strip() or None)
    trans_raw = (attrs.get("transmission_type_name") or attrs.get("transmission") or "").strip() or None
    transmission = normalize_transmission(trans_raw)

    narrative = "\n\n".join(p for p in (title, short_d, full_d) if p)
    if not vin:
        vin = extract_vin_from_description(narrative)
    if not transmission:
        transmission = extract_transmission_from_description(narrative)
    # No raw-string fallback — the DB ``transmission`` column is a strict
    # ``Manual`` / ``Automatic`` / ``NULL`` enum, so if the normalizer can't
    # classify the source text we leave it blank rather than leak noise.

    return {
        "record_id": lot_id,
        "title": title,
        "year": _year(attrs),
        "make": _title_case_phrase(attrs.get("make")),
        "model": _title_case_phrase(_model_line(attrs)),
        "vin": vin,
        "mileage_mi": _mileage_from_narrative(short_d, full_d),
        "ext_color": _title_case_phrase(attrs.get("exterior_color")),
        "int_color": _title_case_phrase(attrs.get("interior_color")),
        "engine_spec": _engine_spec(attrs),
        "transmission": transmission,
        "lot_number": ((attrs.get("lot_number") or "").strip() or None),
        "hammer_usd": hammer if is_sold else None,
        "sold_status": "Sold" if is_sold else "Unsold",
        "selling_date": _selling_date(attrs),
        "image_url": ((attrs.get("main_image_url") or "").strip() or None),
        "listing_url": _listing_url(event_slug, slug),
        "auction_event": auction_label,
        "listing_description": listing_description,
    }
