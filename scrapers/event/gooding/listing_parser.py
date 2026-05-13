"""Gooding & Company — extract lot paths from app.js, parse lot page-data JSON."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shared.normalizer import extract_description_fields, normalize_vin

from .config import IMAGE_CDN_BASE, SITE_ORIGIN

logger = logging.getLogger(__name__)

# Bundled path list: ``[{"path":"/…","matchPath":…},…]``.
# The first entry used to be ``/auction/`` but changes with each deploy — we
# match any ``[{"path":`` opening so the anchor never goes stale.
_PATH_LIST_START_RE = re.compile(r'\[\s*\{\s*"path"\s*:\s*"/')

# ``/lot/<slug>`` — drop trailing slash if any (none in app.js payload).
_LOT_PATH_RE = re.compile(r"^/lot/[a-z0-9][a-z0-9\-]*$", re.I)

# Inline lot-path strings scattered through minified app.js bundles.
# Matches ``"/lot/some-slug"`` or ``'/lot/some-slug'`` anywhere in the JS.
_INLINE_LOT_RE = re.compile(r'["\'](/lot/[a-z0-9][a-z0-9\-]+)["\']', re.I)

# Gooding always titles cars with a leading year: ``1969 Ferrari 365 GTB/4 Daytona``.
_YEAR_LEADING_RE = re.compile(r"^(?:19|20)\d{2}\b")

# Transmission bullet in ``specifications``:
#   ``4-Speed Manual Gearbox``, ``3-Speed Progressive Gearbox``,
#   ``2-Speed Planetary Transmission``, ``4-Speed Manual Transaxle with Dual-Chain Drive``,
#   ``7-Speed PDK Dual-Clutch``, ``6-Speed Automatic``
# Skip lines like ``Transmission Brake`` / ``Rear ... Transmission Brakes`` (brake, not gearbox).
_TRANS_SPEED_RE = re.compile(
    r"(?P<speeds>\d+)\s*[-–]?\s*Speed\b[^.\n]*?(?:Gearbox|Transmission|Transaxle|Manual|Automatic|Auto|PDK|DCT|CVT|Dual[-\s]?Clutch|Semi[-\s]?Auto)[^.\n]*",
    re.I,
)
_TRANS_KEYWORD_RE = re.compile(
    r"\b(?:PDK|DCT|CVT|Tiptronic|Sportmatic|Powerglide|Hydra[-\s]?Matic|Semi[-\s]?Automatic)\b[^.\n]*",
    re.I,
)
_TRANS_BRAKE_RE = re.compile(r"transmission\s+brake", re.I)


def extract_paths_from_app_js(js_text: str) -> list[dict[str, str]]:
    """Find and parse the embedded ``[{path, matchPath}, …]`` JSON array in the bundle.

    Falls back to inline regex scanning when the JSON array cannot be parsed
    (e.g. the bundle has been minified in a way that breaks balanced-bracket
    extraction, or the first entry is no longer ``/auction/``).
    """
    m = _PATH_LIST_START_RE.search(js_text)
    if m:
        start = m.start()
        depth = 0
        i = start
        n = len(js_text)
        while i < n:
            c = js_text[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    raw = js_text[start : i + 1]
                    try:
                        entries = json.loads(raw)
                        if entries:
                            return entries
                    except json.JSONDecodeError as e:
                        logger.warning("Failed to parse app.js path list JSON: %s", e)
                    break
            i += 1

    # Fallback: scan the whole bundle for quoted ``/lot/<slug>`` strings.
    # This catches bundles where the structured array is absent or malformed.
    logger.info("app.js JSON array parse failed — falling back to inline /lot/ scan")
    seen: set[str] = set()
    entries_fallback: list[dict[str, str]] = []
    for fm in _INLINE_LOT_RE.finditer(js_text):
        p = fm.group(1)
        if p not in seen:
            seen.add(p)
            entries_fallback.append({"path": p})
    if entries_fallback:
        logger.info("Inline scan found %s /lot/ paths", len(entries_fallback))
    return entries_fallback


def parse_lot_paths_from_lots_page(payload: dict | None) -> list[str]:
    """Extract ``/lot/<slug>`` paths from the ``/page-data/lots/page-data.json`` response.

    Gooding's lots catalog page embeds an ``allContentfulLot`` (or similar) node
    array. We walk the result tree looking for slug strings and build paths from them.
    Returns a deduplicated list of ``/lot/<slug>`` strings.
    """
    if not payload:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _walk(obj):
        if isinstance(obj, dict):
            # A node with a ``slug`` that looks like a lot slug.
            slug = obj.get("slug") or obj.get("path") or ""
            if isinstance(slug, str) and _LOT_PATH_RE.match(f"/lot/{slug}") and slug not in seen:
                seen.add(slug)
                out.append(f"/lot/{slug}")
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)
    return out


def select_lot_paths(entries: list[dict[str, str]]) -> list[str]:
    """Keep only ``/lot/<slug>`` entries (drop auctions, auth, 404, etc.)."""
    out: list[str] = []
    seen: set[str] = set()
    for e in entries:
        p = (e or {}).get("path") or ""
        if not p or p in seen:
            continue
        if _LOT_PATH_RE.match(p):
            seen.add(p)
            out.append(p)
    return out


def _image_url_from_public_id(public_id: str | None) -> str | None:
    if not public_id:
        return None
    # Cloudinary accepts the raw public_id path — spaces must stay percent-encoded.
    from urllib.parse import quote

    return f"{IMAGE_CDN_BASE}/{quote(public_id, safe='/')}"


def _auction_date_from_subevents(subevents: list[dict[str, Any]] | None) -> str | None:
    for ev in subevents or []:
        if (ev.get("__typename") or "") == "ContentfulSubEventAuction":
            start = ev.get("startDate")
            if start and isinstance(start, str):
                return start[:10]
    return None


def _clean_trans(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" .,;")


_MANUAL_HINTS_RE = re.compile(
    r"\b(?:Manual|Stick[-\s]?Shift|Gearbox|Transaxle|Selective[-\s]?Sliding|H[-\s]?Pattern|Planetary|Progressive|Sequential)\b",
    re.I,
)
_AUTO_HINTS_RE = re.compile(
    r"\b(?:Automatic|Auto(?!mobil)|PDK|DCT|CVT|Tiptronic|Sportmatic|Powerglide|Hydra[-\s]?Matic|Dual[-\s]?Clutch|Semi[-\s]?Auto)\b",
    re.I,
)


def parse_transmission_from_specs(specs: list[str]) -> str | None:
    """Return ``"Manual"`` or ``"Automatic"`` scanning ``specifications`` for a gearbox line."""
    if not specs:
        return None
    for line in specs:
        if not isinstance(line, str):
            continue
        if _TRANS_BRAKE_RE.search(line) and not _TRANS_SPEED_RE.search(line):
            # ``Transmission Brake`` alone is a brake, not a gearbox — skip.
            continue
        if not (_TRANS_SPEED_RE.search(line) or _TRANS_KEYWORD_RE.search(line)):
            continue
        if _AUTO_HINTS_RE.search(line):
            return "Automatic"
        if _MANUAL_HINTS_RE.search(line):
            return "Manual"
    return None


_MILEAGE_RE = re.compile(
    r"(?:^|[\s(])(\d{1,7})\s*(?:original|actual|indicated|documented|showing|approximately|approx\.?)?\s*(?:mi\.?|miles?)\b",
    re.I,
)
_KM_RE = re.compile(r"(?:^|[\s(])(\d{1,7})\s*(?:km|kilometers?|kilometres?)\b", re.I)


_CHASSIS_SPEC_RE = re.compile(
    r"^Chassis\s*(?:No\.?|Number|#)?\s*[:\-]?\s*(.+)$",
    re.I,
)


def _chassis_from_specs(item: dict[str, Any]) -> str | None:
    """Extract chassis number from structured field or specifications/highlights list."""
    # Structured field first (some lots have it)
    for key in ("chassis", "chassisNumber", "chassisNo", "vin", "serialNumber"):
        val = item.get(key)
        if val and isinstance(val, str) and val.strip():
            raw = val.strip()
            v = normalize_vin(raw)
            if v:
                return v
            # Vintage chassis like "0202 A" have a space — try compacted form
            compact = re.sub(r"\s+", "", raw).upper()
            if compact:
                v = normalize_vin(compact)
                if v:
                    return v
            # Return raw — e.g. "0202 A", "J-386" don't pass VIN validation
            if len(raw) >= 2:
                return raw

    # Scan specifications and highlights lines for "Chassis No. XYZ"
    for field in ("specifications", "highlights"):
        for line in (item.get(field) or []):
            if not isinstance(line, str):
                continue
            m = _CHASSIS_SPEC_RE.match(line.strip())
            if m:
                raw = m.group(1).strip()
                # Normalize: strip punctuation noise, try as VIN/chassis
                v = normalize_vin(re.sub(r"[\s*]+", "", raw).upper())
                if v:
                    return v
                # For short chassis like "97" or "1215 GT" normalize_vin may reject
                # (too short or all-digit). Return the raw cleaned string directly.
                cleaned = re.sub(r"\s+", " ", raw).strip()
                if cleaned:
                    return cleaned
    return None


_SPEC_COLON_RE = re.compile(r"^[^:]{2,50}:\s*(.+)$")
_INTERIOR_STRIP_RE = re.compile(
    r"\s*\b(?:leather|alcantara|suede|cloth|fabric|upholstery)\b.*$", re.I,
)


def _colors_from_specs(specs: list[str]) -> tuple[str | None, str | None]:
    """Scan structured specifications lines for exterior and interior color.

    Handles Gooding's typical formats:
      ``Exterior Color: Rosso Corsa``
      ``Interior: Black Leather``
      ``Presentation: Black Leather Over Red Carpet``
    """
    ext_color: str | None = None
    int_color: str | None = None
    for line in specs:
        if not isinstance(line, str) or not line.strip():
            continue
        ll = line.lower()
        if not ext_color:
            if re.match(r"exterior\s*(?:colou?r)?", ll):
                m = _SPEC_COLON_RE.match(line)
                if m:
                    ext_color = m.group(1).strip()
            elif re.match(r"(?:painted|finish(?:ed)?|colour?r?)\s*:", ll):
                m = _SPEC_COLON_RE.match(line)
                if m:
                    ext_color = m.group(1).strip()
        if not int_color:
            if re.match(r"interior\s*(?:colou?r)?", ll):
                m = _SPEC_COLON_RE.match(line)
                if m:
                    val = _INTERIOR_STRIP_RE.sub("", m.group(1)).strip()
                    if val:
                        int_color = val
            elif re.match(r"upholstery\s*:", ll):
                m = _SPEC_COLON_RE.match(line)
                if m:
                    val = m.group(1).strip()
                    if val:
                        int_color = val
            elif not int_color:
                # "Black leather interior" / "Tan Alcantara upholstery"
                mi = re.match(
                    r"([A-Za-z][A-Za-z\s\-]{1,30}?)\s+"
                    r"(?:leather|alcantara|suede|cloth|fabric)\s+"
                    r"(?:interior|upholstery|trim)",
                    line, re.I,
                )
                if mi:
                    int_color = mi.group(1).strip()
    return ext_color, int_color


def _parse_mileage_from_specs(specs: list[str], description: str = "") -> int | None:
    """Extract mileage from specifications lines or description prose."""
    # Scan specification lines first for "X miles" or "X km" patterns.
    for line in specs:
        if not isinstance(line, str):
            continue
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", line)
        m = _MILEAGE_RE.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        m = _KM_RE.search(text)
        if m:
            try:
                return round(int(m.group(1)) * 0.621371)
            except ValueError:
                pass
    # Fall back to description prose.
    if description:
        text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", description)
        m = _MILEAGE_RE.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        m = _KM_RE.search(text)
        if m:
            try:
                return round(int(m.group(1)) * 0.621371)
            except ValueError:
                pass
    return None


def _join_description(item: dict[str, Any]) -> str | None:
    highlights = [h for h in (item.get("highlights") or []) if isinstance(h, str) and h.strip()]
    specs = [s for s in (item.get("specifications") or []) if isinstance(s, str) and s.strip()]
    parts: list[str] = []
    if highlights:
        parts.append("Highlights:\n" + "\n".join(f"• {h.strip()}" for h in highlights))
    if specs:
        parts.append("Specifications:\n" + "\n".join(f"• {s.strip()}" for s in specs))
    return "\n\n".join(parts) if parts else None


def parse_lot_page_data(payload: dict[str, Any], *, path: str) -> dict[str, Any] | None:
    """Flatten Gatsby ``page-data.json`` for a lot into a single merged dict."""
    result = (payload or {}).get("result") or {}
    data = result.get("data") or {}
    lot = data.get("contentfulLot") or {}
    if not lot:
        return None

    item = lot.get("item") or {}
    item_typename = (item.get("__typename") or "").strip()
    item_type = (item.get("type") or "").strip()

    auction = lot.get("auction") or {}
    auction_name = auction.get("name")
    currency = (auction.get("currency") or "USD").upper()
    selling_date = _auction_date_from_subevents(auction.get("subEvents"))

    imgs = item.get("cloudinaryImagesCombined") or []
    first_pid = None
    if imgs and isinstance(imgs[0], dict):
        first_pid = imgs[0].get("public_id")
    image_url = _image_url_from_public_id(first_pid)

    year = item.get("modelYear")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    make_obj = item.get("make") or {}
    make = make_obj.get("name") if isinstance(make_obj, dict) else None

    model = item.get("model") or None

    title = item.get("title") or lot.get("title") or ""
    if not title and year and make:
        title = f"{year} {make} {model or ''}".strip()

    transmission = parse_transmission_from_specs(item.get("specifications") or [])

    description_text = _join_description(item)

    # Extract colors: structured specs list first (most reliable for Gooding),
    # then fall back to description-text extraction.
    ext_color, int_color = _colors_from_specs(item.get("specifications") or [])
    if not ext_color or not int_color:
        desc_fields = extract_description_fields(description_text or "") if description_text else {}
        ext_color = ext_color or desc_fields.get("ext_color")
        int_color = int_color or desc_fields.get("int_color")

    mileage_mi = _parse_mileage_from_specs(item.get("specifications") or [], description_text or "")

    sale_price = lot.get("salePrice")
    try:
        if sale_price is not None and float(sale_price) <= 1.0:
            sale_price = None
    except (TypeError, ValueError):
        pass
    low = lot.get("lowEstimate")
    high = lot.get("highEstimate")

    slug = lot.get("slug") or path.rsplit("/", 1)[-1]
    listing_url = f"{SITE_ORIGIN}{path}/"

    # Engine spec: Gooding's ``engine`` CMS field stores the ENGINE SERIAL number for
    # vintage lots (e.g. "1825", "198.980.10.9500068") — NOT a displacement description.
    # The actual engine description is always specifications[0], e.g.:
    #   "5,733 CC DOHC V-10 Engine" / "2,953 CC SOHC V-12 Engine"
    # Strategy: find the first spec/highlight line that contains an engine-describing
    # keyword (displacement unit, cylinder config, etc.) and use it directly.
    _ENGINE_LINE_RE = re.compile(
        r"\b(?:cc|cyl(?:inder)?|litre?|liter|cubic|v[-\s]?\d{1,2}\b|inline|straight|flat[-\s]?\d|"
        r"electric|hybrid|turbo|supercharg|engine\b)",
        re.I,
    )
    from shared.normalizer import extract_description_fields as _edf
    engine_spec: str | None = None
    for _line in (item.get("specifications") or []) + (item.get("highlights") or []):
        if not isinstance(_line, str):
            continue
        if _ENGINE_LINE_RE.search(_line):
            engine_spec = _line.strip()
            break
    if not engine_spec:
        specs_text = " ".join(
            s for s in (item.get("specifications") or []) + (item.get("highlights") or [])
            if isinstance(s, str)
        )
        if specs_text:
            engine_spec = _edf(specs_text).get("engine")

    return {
        "record_id": f"gooding_{slug}",
        "slug": slug,
        "listing_url": listing_url,
        "path": path,
        "title": title or None,
        "year": year,
        "make": make,
        "model": model,
        "item_type": item_type or None,
        "item_typename": item_typename or None,
        "vin": _chassis_from_specs(item),
        "engine_spec": engine_spec,
        "transmission": transmission,
        "ext_color": ext_color,
        "int_color": int_color,
        "mileage_mi": mileage_mi,
        "lot_number": str(lot.get("lotNumber")) if lot.get("lotNumber") is not None else None,
        "auction_name": auction_name,
        "currency": currency,
        "selling_date": selling_date,
        "sale_price": sale_price,
        "estimate_low": low,
        "estimate_high": high,
        "has_reserve": bool(lot.get("hasReservePrice")),
        "image_url": image_url,
        "description": _join_description(item),
    }
