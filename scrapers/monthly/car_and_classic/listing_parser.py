"""Parse Car & Classic Inertia search rows + listing partial JSON."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import country_code_to_name, normalize_engine_spec, normalize_transmission

from .config import SITE_ORIGIN


def _abs_url(path: str | None) -> str | None:
    if not path or not str(path).strip():
        return None
    p = str(path).strip()
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if p.startswith("/"):
        return f"{SITE_ORIGIN}{p}"
    return f"{SITE_ORIGIN}/{p}"


def _image_url(row: dict[str, Any]) -> str | None:
    imgs = row.get("images")
    if not isinstance(imgs, list) or not imgs:
        return None
    first = imgs[0]
    if isinstance(first, dict):
        return _abs_url(first.get("url"))
    return None


def gbp_minor_to_major(price_block: dict[str, Any] | None) -> int | None:
    """List/search ``price.value`` is in pence (e.g. ``1450000`` → £14,500)."""
    if not isinstance(price_block, dict):
        return None
    v = price_block.get("value")
    try:
        return int(v) // 100
    except (TypeError, ValueError):
        return None


def _parse_iso_date(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:10]
    return None


# Title often includes hyphenated marque; ``make`` from API may be a short form (e.g. ``Mercedes``).
_MULTIWORD_BRANDS = (
    "Mercedes-Benz",
    "Land Rover",
    "Alfa Romeo",
    "Aston Martin",
    "Rolls-Royce",
    "Rolls Royce",
    "Austin Healey",
    "Range Rover",
    "AM General",
)


def _strip_brand_prefix(rest: str, make: str | None) -> str:
    t = (rest or "").strip()
    if not t or not make:
        return t
    tlow = t.lower()
    for brand in sorted(_MULTIWORD_BRANDS, key=len, reverse=True):
        bl = brand.lower()
        if tlow.startswith(bl + " "):
            return t[len(brand) :].strip()
    m = make.strip()
    if tlow.startswith(m.lower() + " "):
        return t[len(m) :].strip()
    return t


def split_title(title: str, make: str | None, year_s: str | None) -> tuple[int | None, str | None, str | None]:
    year: int | None = None
    if year_s is not None and str(year_s).strip().isdigit():
        year = int(str(year_s).strip())
    t = (title or "").strip()
    if year is not None and t.lower().startswith(str(year)):
        t = t[len(str(year)) :].strip()
    t = _strip_brand_prefix(t, make)
    model = t.strip() or None
    return year, make, model


def _attrs(row: dict[str, Any]) -> dict[str, Any]:
    a = row.get("attributes")
    return a if isinstance(a, dict) else {}


# Values that are fuel/energy labels, not displacement text.
_FUEL_ONLY_TERMS = frozenset(
    {
        "petrol",
        "gasoline",
        "diesel",
        "hybrid",
        "plug-in hybrid",
        "plug in hybrid",
        "phev",
        "mhev",
        "hev",
        "lpg",
        "cng",
        "biofuel",
        "electric",
        "ev",
    }
)

_JSON_VRM_PATTERNS = (
    re.compile(r'["\']vrm["\']\s*:\s*["\']([^"\']{3,12})["\']', re.I),
    re.compile(r'["\']registration(?:Number)?["\']\s*:\s*["\']([^"\']{3,12})["\']', re.I),
    re.compile(r'["\']vehicleRegistrationMark["\']\s*:\s*["\']([^"\']{3,12})["\']', re.I),
)

_REG_NEAR_LABEL_RE = re.compile(
    r"(?:registration|reg\.?\s*(?:no|number)?|vrm|number\s*plate)\s*[.:]\s*"
    r"([A-HJ-NPR-Z0-9]{2,4}\s*[A-HJ-NPR-Z0-9]{2,4}\s*[A-HJ-NPR-Z0-9]{0,4})",
    re.I,
)


def _normalize_vrm_candidate(raw: str) -> str | None:
    """Return compact UK-style registration if ``raw`` looks like a plate, else None."""
    t = re.sub(r"\s+", "", (raw or "").strip().upper())
    if len(t) < 4 or len(t) > 10:
        return None
    if not t.isalnum():
        return None
    if not any(c.isdigit() for c in t) or not any(c.isalpha() for c in t):
        return None
    # Long all-hex strings are usually ids, not plates.
    if len(t) >= 8 and all(c in "0123456789ABCDEF" for c in t):
        return None
    return t


def _extract_vrm_from_html(html: str) -> str | None:
    """
    Pull a likely UK VRM from raw listing HTML when Inertia JSON is missing ``vehicle``.

    Order: embedded JSON keys (``vrm``, ``registration``), then visible ``Registration:`` style text.
    """
    blob = str(html)[:800_000]
    for cre in _JSON_VRM_PATTERNS:
        for m in cre.finditer(blob):
            v = _normalize_vrm_candidate(m.group(1))
            if v:
                return v
    for m in _REG_NEAR_LABEL_RE.finditer(blob):
        v = _normalize_vrm_candidate(m.group(1))
        if v:
            return v
    return None


def _vehicle_vrm(vehicle: dict[str, Any]) -> str | None:
    for key in (
        "vrm",
        "VRM",
        "registration",
        "registrationNumber",
        "numberPlate",
        "vehicleRegistrationMark",
    ):
        s = vehicle.get(key)
        if s is None:
            continue
        t = str(s).strip()
        if t:
            n = _normalize_vrm_candidate(t) or t.upper().replace(" ", "")
            if n and len(n) <= 12:
                return n
    return None


def enrich_listing_partial_with_document_vrm(partial: dict[str, Any], html: str) -> dict[str, Any]:
    """
    When Inertia JSON has no usable plate on ``vehicle``, copy VRM parsed from the document HTML.

    The detail page HTML often still embeds ``vrm`` / ``registration`` even if the partial omits ``vehicle``.
    """
    if not html or not str(html).strip():
        return partial
    props = partial.get("props")
    if not isinstance(props, dict):
        return partial
    listing = props.get("listing")
    if not isinstance(listing, dict):
        return partial
    vehicle = listing.get("vehicle") if isinstance(listing.get("vehicle"), dict) else {}
    if _vehicle_vrm(vehicle):
        return partial
    vrm = _extract_vrm_from_html(html)
    if not vrm:
        return partial
    out = dict(partial)
    out_props = dict(props)
    out_listing = dict(listing)
    out_veh = dict(vehicle) if vehicle else {}
    out_veh["vrm"] = vrm
    out_listing["vehicle"] = out_veh
    out_props["listing"] = out_listing
    out["props"] = out_props
    return out


def _mileage_mi(attrs: dict[str, Any]) -> int | None:
    m = attrs.get("mileage")
    if not isinstance(m, dict):
        return None
    try:
        val = int(m.get("value"))
    except (TypeError, ValueError):
        return None
    unit = (m.get("unit") or "mi").lower()
    if unit in ("km", "kilometers", "kilometre", "kilometres"):
        return round(val * 0.621371)
    return val


def _format_engine_cc_liters(engine_size: Any, fuel_type: str | None) -> str | None:
    """
    Car & Classic uses displacement as a whole number in **cc** (e.g. ``2800``, ``1796``).

    Output is **displacement only** (e.g. ``2.8-Liter``); fuel is not appended to ``Engine_spec``.
    ``fuelType`` is still read so purely-electric rows can map to ``Electric``.
    """
    ft = (str(fuel_type).strip().lower() if fuel_type else "") or ""
    es = str(engine_size).strip() if engine_size is not None and str(engine_size).strip() else ""

    if ft in ("electric", "ev") or es.lower() in ("electric", "ev"):
        return "Electric"

    if not es and not ft:
        return None

    esl = es.lower()
    if es and esl in _FUEL_ONLY_TERMS:
        es = ""

    num_part: str | None = None
    if es and es.replace(".", "", 1).isdigit():
        num = float(es)
        if 299 < num <= 20000:
            liters = num / 1000.0
            num_part = f"{liters:.1f}-Liter"
        elif 0.4 <= num <= 12.5:
            lit_s = f"{num:g}"
            num_part = f"{lit_s}-Liter"
        else:
            num_part = es
    elif es:
        num_part = es

    if not num_part:
        return None
    raw = num_part
    return normalize_engine_spec(raw) or raw


def _html_to_text(html: str | None) -> str | None:
    if not html or not str(html).strip():
        return None
    soup = BeautifulSoup(str(html), "lxml")
    txt = soup.get_text("\n", strip=True)
    return txt[:200_000] if txt else None


def parse_search_row(row: dict[str, Any]) -> dict[str, Any]:
    """One element from ``props.searchResults.data``."""
    rid = str(row.get("id") or "").strip()
    title = (row.get("title") or "").strip()
    make = (row.get("make") or "").strip() or None
    year_s = row.get("year")
    year, make2, model = split_title(title, make, str(year_s) if year_s is not None else None)
    make = make2 or make

    url = (row.get("url") or "").strip()
    attrs = _attrs(row)
    auction = row.get("auction") if isinstance(row.get("auction"), dict) else {}
    sold = bool(row.get("isSold"))

    sold_status = "Sold" if sold else "Unsold"
    amt = gbp_minor_to_major(row.get("price"))

    tx = normalize_transmission(attrs.get("transmissionType"))
    engine = _format_engine_cc_liters(attrs.get("engineSize"), attrs.get("fuelType"))

    loc = row.get("location") if isinstance(row.get("location"), dict) else {}
    country = country_code_to_name(loc.get("countryCode"))

    return {
        "record_id": rid,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "sold_status": sold_status,
        "amount_gbp_major": amt,
        "currency": "GBP",
        "bid_count": auction.get("bidCount"),
        "selling_date": _parse_iso_date(auction.get("endDatetime")),
        "lot_number": row.get("id"),
        "image_url": _image_url(row),
        "listing_url": _abs_url(url),
        "auction_path": url if url.startswith("/") else f"/{url}",
        "country": country,
        "mileage_mi": _mileage_mi(attrs),
        "ext_color": (attrs.get("colour") or "").strip() or None,
        "engine_spec": engine,
        "transmission": tx,
        "reserve_status": auction.get("reserveStatus"),
    }


def listing_payload_from_document_html(html: str | None) -> dict[str, Any] | None:
    """
    When Inertia partial JSON is blocked (403), use the full document HTML we already fetched.

    Returns the same outer shape as an Inertia response: ``{"props": {"listing": {...}}}``.
    """
    if not html or not str(html).strip():
        return None
    soup = BeautifulSoup(html, "lxml")
    desc = None
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        desc = str(og["content"]).strip()
    if not desc:
        nm = soup.find("meta", attrs={"name": "description"})
        if nm and nm.get("content"):
            desc = str(nm["content"]).strip()

    vrm = _extract_vrm_from_html(html)
    listing: dict[str, Any] = {}
    if desc:
        safe = html_lib.escape(desc)
        listing["description"] = f"<p>{safe}</p>"
    if vrm:
        listing["vehicle"] = {"vrm": vrm}
    if not listing:
        return {"props": {"listing": {}}}
    return {"props": {"listing": listing}}


def merge_listing_partial(summary: dict[str, Any], partial: dict[str, Any] | None) -> dict[str, Any]:
    """
    Merge ``listing`` prop from Inertia partial over search row.

    Ended-auction exports intentionally leave **estimate** columns empty: site ``guidePrice``
    is a pre-sale guide, not the hammer result. UK **VRM** from ``vehicle.vrm`` is stored in the
    **VIN** column (schema field) when present.
    """
    out = dict(summary)
    if not partial:
        return out
    props = partial.get("props") if isinstance(partial, dict) else None
    if not isinstance(props, dict):
        return out
    listing = props.get("listing")
    if not isinstance(listing, dict):
        return out

    vehicle = listing.get("vehicle") if isinstance(listing.get("vehicle"), dict) else {}
    if vehicle.get("transmissionLabel") and not out.get("transmission"):
        out["transmission"] = normalize_transmission(vehicle.get("transmissionLabel"))

    v_eng = _format_engine_cc_liters(vehicle.get("engineSize"), vehicle.get("fuelType"))
    if v_eng:
        out["engine_spec"] = v_eng

    if vehicle.get("colour") and not (out.get("ext_color") or "").strip():
        out["ext_color"] = str(vehicle["colour"]).strip()

    vm = vehicle.get("mileage")
    if isinstance(vm, dict) and out.get("mileage_mi") is None:
        mi = _mileage_mi({"mileage": vm})
        if mi is not None:
            out["mileage_mi"] = mi

    if not (out.get("int_color") or "").strip():
        for src in (vehicle, listing):
            if not isinstance(src, dict):
                continue
            for _ik in ("interiorColour", "interiorColor", "interior"):
                iv = src.get(_ik)
                if iv and str(iv).strip():
                    out["int_color"] = str(iv).strip()
                    break
            if out.get("int_color"):
                break

    vrm = _vehicle_vrm(vehicle)
    if vrm:
        out["vin"] = vrm

    desc_parts: list[str] = []
    body = _html_to_text(listing.get("description"))
    if body:
        desc_parts.append(body)

    if desc_parts:
        out["listing_description"] = "\n\n".join(desc_parts)

    return out
