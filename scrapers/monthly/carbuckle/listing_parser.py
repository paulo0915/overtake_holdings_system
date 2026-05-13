"""Parse Carbuckle Typesense document records."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from shared.normalizer import KM_TO_MILES, normalize_transmission

from .config import BASE_URL, IMAGE_CDN_BASE

# ── Mileage ────────────────────────────────────────────────────────────────────
_K_MILES_RE = re.compile(r"([\d.]+)\s*k", re.I)
_NUM_RE = re.compile(r"[\d,]+")


def _parse_mileage_raw(raw: Any) -> int | None:
    """Parse mileage field which can be int, float, or string like '1,800' / '25k-miles'."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    # "25k-miles", "25k miles", "25k"
    m = _K_MILES_RE.search(s)
    if m:
        try:
            return int(round(float(m.group(1)) * 1000))
        except ValueError:
            return None
    # plain number with optional commas
    m = _NUM_RE.search(s)
    if m:
        try:
            return int(m.group(0).replace(",", ""))
        except ValueError:
            return None
    return None


def parse_mileage(doc: dict[str, Any]) -> int | None:
    """Return mileage in miles, converting from km when indicated or implied.

    Carbuckle is a Canadian platform so the default unit is km.  The
    ``facet_isInMiles`` / ``isInMiles`` flags are set explicitly when the
    seller specifies miles; otherwise we treat the raw value as km.
    """
    raw = doc.get("facet_mileage") or doc.get("mileage")
    val = _parse_mileage_raw(raw)
    if val is None:
        return None
    # Prefer explicit unit flags (both prefixed and non-prefixed variants)
    in_miles = doc.get("isInMiles") if doc.get("isInMiles") is not None else doc.get("facet_isInMiles")
    if in_miles is True:
        return val
    if in_miles is False:
        return int(round(val * KM_TO_MILES))
    # Unknown unit — check if the raw string explicitly mentions "mile"
    if "mile" in str(raw).lower():
        return val
    # Canadian platform default: assume km and convert to miles
    return int(round(val * KM_TO_MILES))


# ── Currency ───────────────────────────────────────────────────────────────────
_CURRENCY_MAP = {
    "cad$": "CAD",
    "usd$": "USD",
    "eur€": "EUR",
    "gbp£": "GBP",
    "cad": "CAD",
    "usd": "USD",
    "eur": "EUR",
    "gbp": "GBP",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
}


def parse_currency(raw: str | None) -> str:
    if not raw:
        return "CAD"
    return _CURRENCY_MAP.get(raw.lower().strip(), raw.strip().upper())


# ── Date ───────────────────────────────────────────────────────────────────────
def parse_date_ms(ms: int | float | None) -> str | None:
    """Unix millisecond timestamp → YYYY-MM-DD (UTC)."""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _coerce_timestamp_to_day(val: Any) -> str | None:
    """Typesense ms int, Firestore ISO string, or numeric string → YYYY-MM-DD (UTC)."""
    if val is None or val is False:
        return None
    if isinstance(val, (int, float)) and val > 0:
        return parse_date_ms(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if s.isdigit():
            try:
                return parse_date_ms(int(s))
            except (OSError, OverflowError, ValueError):
                return None
        try:
            if s.endswith("Z"):
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def parse_selling_date(doc: dict[str, Any]) -> str | None:
    """Best-effort **sold** calendar date (UTC), not the scheduled listing end.

    Priority:
    1. ``lastBidTime`` — when the winning bid / accepted offer was recorded (closest to sale)
    2. ``dateIndexed`` — when the listing was indexed as sold (good when no bid row)
    3. ``dateEdited`` — last document update (often sale-related metadata)
    4. ``dateEnd`` — scheduled auction/listing end (fallback only)
    """
    for key in ("lastBidTime", "dateIndexed", "dateEdited", "dateEnd"):
        d = _coerce_timestamp_to_day(doc.get(key))
        if d:
            return d
    return None


# ── Price ──────────────────────────────────────────────────────────────────────
def parse_price(doc: dict[str, Any]) -> tuple[int | None, str]:
    """
    Return (amount, currency_code).

    Priority:
    - AUCTION: ``lastBidValue``
    - OFFER: ``priceDisplay`` if set, else ``lastBidValue`` (accepted offer is stored
      as the final bid; ``priceDisplay`` is often 0 on OFFER-SOLD listings)
    - CLASSIFIED / unknown: ``priceBuy`` → ``priceDisplay`` → ``lastBidValue``
    """
    sold_by = (doc.get("soldBy") or "").upper()
    cur = parse_currency(doc.get("currency"))
    try:
        lbv = int(float(doc.get("lastBidValue") or 0))
    except (TypeError, ValueError):
        lbv = 0
    try:
        pdv = int(float(doc.get("priceDisplay") or 0))
    except (TypeError, ValueError):
        pdv = 0
    pb_raw = doc.get("priceBuy")
    try:
        pbv = int(float(pb_raw)) if pb_raw not in (None, False, "") else 0
    except (TypeError, ValueError):
        pbv = 0

    if sold_by == "AUCTION":
        return (lbv if lbv else None), cur
    if sold_by == "OFFER":
        amt = pdv or lbv or pbv
        return (amt if amt else None), cur
    # CLASSIFIED or unknown soldBy — prefer priceBuy (asking/sold price for classifieds)
    amt = pbv or pdv or lbv
    return (amt if amt else None), cur


# ── VIN ────────────────────────────────────────────────────────────────────────
_VIN_PLACEHOLDERS = frozenset({"N/A", "NA", "TBD", "TBC", "NONE", "UNKNOWN", "-", ""})


def _validate_vin_string(raw: str) -> str | None:
    """Clean and uppercase a VIN/chassis string, stripping strange chars."""
    cleaned = re.sub(r"[^a-zA-Z0-9\-./]", "", raw.strip()).strip("-./").upper()
    if not cleaned or cleaned in _VIN_PLACEHOLDERS or len(cleaned) > 25:
        return None
    return cleaned


def parse_vin(doc: dict[str, Any]) -> str | None:
    # Try all known key names first (explicit priority order).
    for key in ("facet_vin", "facet_VIN#", "facet_VIN", "vin", "facet_serial", "serial"):
        v = doc.get(key)
        if v:
            result = _validate_vin_string(str(v))
            if result:
                return result
    # Case-insensitive scan: catches any key whose name contains "vin" or "serial"
    # regardless of capitalization or prefix (e.g. "facet_VIN#", "VIN #", "chassis").
    for key, v in doc.items():
        if not v:
            continue
        kl = key.lower().replace(" ", "").replace("#", "").replace("_", "")
        if kl in ("vin", "facetvin", "serial", "facetserial", "chassis", "facetchassis"):
            result = _validate_vin_string(str(v))
            if result:
                return result
    return None


# ── Image ──────────────────────────────────────────────────────────────────────
def parse_image_url(doc: dict[str, Any]) -> str | None:
    raw = doc.get("images")
    if not raw:
        return None
    try:
        imgs = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not imgs:
        return None
    path = imgs[0].strip()
    if not path:
        return None
    return f"{IMAGE_CDN_BASE}{quote(path, safe='')}"


# ── Location ──────────────────────────────────────────────────────────────────
def parse_location(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (location_string, country)."""
    label = (doc.get("locationLabel") or "").strip()
    country = (doc.get("country") or "").strip() or None

    if label:
        return label, country or _infer_country_from_label(label)
    # Reconstruct from parts
    parts = [p for p in (doc.get("city"), doc.get("province"), doc.get("country")) if p]
    loc = ", ".join(str(p) for p in parts) if parts else None
    return loc, country


def _infer_country_from_label(label: str) -> str | None:
    l = label.lower()
    if "canada" in l:
        return "Canada"
    if "united states" in l or ", usa" in l.replace(".", "").lower():
        return "USA"
    return None


# ── Transmission / Engine ─────────────────────────────────────────────────────
def parse_transmission(doc: dict[str, Any]) -> str | None:
    raw = doc.get("facet_transmission")
    if raw:
        return normalize_transmission(str(raw).strip())
    # facet_isAutomatic boolean is set by the "Automatic: Yes/No" detail-page spec.
    # Use explicit None check — `or` short-circuits on False and loses the signal.
    is_auto = doc.get("facet_isAutomatic")
    if is_auto is None:
        is_auto = doc.get("isAutomatic")
    if is_auto is True:
        return "automatic"
    if is_auto is False:
        return "manual"
    return None


def parse_engine(doc: dict[str, Any]) -> str | None:
    raw = doc.get("facet_motor")
    return str(raw).strip() if raw else None


# ── Main document parser ───────────────────────────────────────────────────────
def parse_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw Typesense document into a normalised row dict."""
    lid = doc.get("listingId") or doc.get("id") or ""
    path = doc.get("path") or ""

    year_raw = doc.get("year")
    year = int(year_raw) if isinstance(year_raw, (int, float)) and year_raw else None

    make = (doc.get("make") or "").strip() or None
    model = (doc.get("model") or "").strip() or None
    title = (doc.get("title") or "").strip()
    if not title and all((year, make, model)):
        title = f"{year} {make} {model}"

    amt, cur = parse_price(doc)
    status = "Sold" if doc.get("isSold") else "Unsold"
    selling_date = parse_selling_date(doc)
    location, country = parse_location(doc)

    engine = parse_engine(doc)
    transmission = parse_transmission(doc)
    desc = _build_description(doc)

    # Text-extraction fallback for listings whose facet fields are absent
    if desc and not engine:
        engine = _extract_engine_from_text(desc)
    if desc and not transmission:
        transmission = _extract_transmission_from_text(desc)

    return {
        "record_id": lid,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "vin": parse_vin(doc),
        "mileage_mi": parse_mileage(doc),
        "ext_color": (doc.get("facet_exteriorColor") or "").strip() or None,
        "int_color": (doc.get("facet_interiorColor") or "").strip() or None,
        "engine_spec": engine,
        "transmission": transmission,
        "sold_status": status,
        "amount": amt,
        "currency": cur,
        "selling_date": selling_date,
        "country": country,
        "location": location,
        "image_url": parse_image_url(doc),
        "listing_url": f"{BASE_URL}/listing/{path}" if path else None,
        "listing_description": desc,
    }


def _build_description(doc: dict[str, Any]) -> str | None:
    """Combine cmsContent with the features tagline for a complete description."""
    cms = (doc.get("cmsContent") or "").strip()
    features = (doc.get("features") or "").strip()
    if cms:
        if features:
            return f"Highlights: {features}\n\n{cms}"
        return cms
    # Fallback: features + basic specs when no full description is available
    parts: list[str] = []
    if features:
        parts.append(f"Features: {features}")
    for key, label in (
        ("facet_motor", "Engine"),
        ("facet_transmission", "Transmission"),
        ("facet_exteriorColor", "Exterior"),
        ("facet_interiorColor", "Interior"),
        ("facet_mileage", "Mileage"),
        ("facet_horsepower", "Horsepower"),
    ):
        v = (doc.get(key) or "").strip()
        if v:
            parts.append(f"{label}: {v}")
    return "\n".join(parts) if parts else None


# ── Firestore helpers ──────────────────────────────────────────────────────────
# Patterns used inside sentence-level extraction (not applied to full text)
_DISPLACEMENT_RE = re.compile(r"\d[\d.]*\s*[\-]?\s*(?:liter|litre|L)\b", re.I)
_ALT_DISP_RE = re.compile(r"\b\d{3}(?:ci|cc|tdi|tbi|cid)\b", re.I)
_CONFIG_RE = re.compile(
    r"\b(V\d{1,2}|inline[\s\-](?:four|five|six|eight|\d)|flat[\s\-](?:four|six|\d)|"
    r"W\d{1,2}|rotary|electric\s+motor|hybrid|turbodiesel|twin[\s\-]turbo\w*)\b",
    re.I,
)
_TRANS_KW_RE = re.compile(
    r"\b(\d[\s\-]?speed|manual|automatic|CVT|DCT|PDK|DSG|SMG|semi[\s\-]auto|"
    r"dual[\s\-]clutch|Cruise[\s\-]O[\s\-]Matic|Tremec|Getrag)\b",
    re.I,
)


def _fs_value(field: dict) -> Any:
    """Extract a Python value from a Firestore typed field dict."""
    for typ, val in field.items():
        if typ == "stringValue":
            return str(val)
        if typ in ("integerValue", "doubleValue"):
            try:
                return int(val) if typ == "integerValue" else float(val)
            except (ValueError, TypeError):
                return val
        if typ == "booleanValue":
            return bool(val)
        if typ == "timestampValue":
            return str(val)
        if typ == "mapValue":
            return {k: _fs_value(v) for k, v in val.get("fields", {}).items()}
        if typ == "arrayValue":
            return [_fs_value(v) for v in val.get("values", [])]
    return None


def flatten_firestore_doc(fs_doc: dict) -> dict[str, Any]:
    """Convert a raw Firestore REST document into a flat plain-Python dict."""
    return {k: _fs_value(v) for k, v in fs_doc.get("fields", {}).items()}


def _sentences(text: str) -> list[str]:
    """Split text into sentences without breaking on decimal points (e.g. "3.2-liter")."""
    # Split on . / ! / ? / newlines, but NOT on digit.digit (decimal)
    parts = re.split(r"(?<!\d)\.(?!\d)|[!?\n]", text)
    return [p.strip() for p in parts if p.strip()]


_STOP_WORD_LEAD = re.compile(r"^(?:this|the|a|an|it|and|with|featuring|equipped|powered\s+by|includes?|has|also|which|here|that|both|its|these|those)\s+", re.I)


def _extract_engine_from_text(text: str) -> str | None:
    """Extract a concise engine spec from free-text description (sentence-aware)."""
    for sent in _sentences(text):
        disp_m = _DISPLACEMENT_RE.search(sent) or _ALT_DISP_RE.search(sent)
        if not disp_m:
            continue
        disp_start = disp_m.start()
        disp_end = disp_m.end()

        # Find a clean start point by scanning backward up to 20 chars
        # for the last word-break before the displacement number
        prefix = sent[max(0, disp_start - 20): disp_start]
        sep_pos = max(prefix.rfind(" "), prefix.rfind(","), prefix.rfind(";"))
        clean_start = (max(0, disp_start - 20) + sep_pos + 1) if sep_pos >= 0 else disp_start

        end = min(len(sent), disp_end + 40)
        snippet = sent[clean_start:end].strip().strip(",()")

        # Strip leading filler (with, this, the, powered by …)
        snippet = _STOP_WORD_LEAD.sub("", snippet).strip().strip(",")

        # Trim at first comma/semicolon (keeps "3.2-liter 260 HP V6 engine" but
        # drops ", in particular, garners …" or ", with 6-speed …")
        cut = re.search(r"[,;]", snippet)
        if cut and cut.start() > 4:
            snippet = snippet[: cut.start()].strip()

        # Remove trailing connector words
        snippet = re.sub(r"\s*,?\s*(?:paired|coupled|mated|which|and|with)\s*$", "", snippet, flags=re.I).strip()

        snippet = snippet.rstrip(".")
        if 4 < len(snippet) < 80:
            return snippet

    # Fallback: config keyword only (V8, inline-six, etc.)
    m = _CONFIG_RE.search(text)
    if m:
        start = max(0, m.start() - 8)
        end = min(len(text), m.end() + 15)
        snippet = text[start:end].strip()
        snippet = re.split(r"[,;.\n]", snippet)[0].strip()
        snippet = _STOP_WORD_LEAD.sub("", snippet).strip()
        if 2 < len(snippet) < 50:
            return snippet
    return None


# Fixed keywords that are unambiguous transmission identifiers.
# Mapped to canonical form because normalize_transmission may not know them.
_TX_FIXED_MAP = {
    "dsg": "automatic (DSG)",
    "pdk": "automatic (PDK)",
    "dct": "automatic (DCT)",
    "cvt": "automatic (CVT)",
    "smg": "manual (SMG)",
    "s-tronic": "automatic (S-tronic)",
    "edc": "automatic (EDC)",
    "powershift": "automatic (Powershift)",
    "tiptronic": "automatic (Tiptronic)",
}
_TX_FIXED = re.compile(r"\b(DSG|PDK|CVT|DCT|SMG|S[\-]tronic|EDC|Powershift|Tiptronic)\b", re.I)

# Pattern-based: "6-speed automatic", "manual transmission", etc.
_TX_PATTERN = re.compile(
    r"\b(\d[\s\-]?speed[\s\-]*(?:manual|automatic)?|"
    r"(?:manual|automatic)[\s\-](?:transaxle|gearbox|transmission)|"
    r"(?:Cruise[\s\-]O[\s\-]Matic|Tremec\s+\w+|Getrag\s+\w+)\s+(?:automatic|manual)?)",
    re.I,
)


def _extract_transmission_from_text(text: str) -> str | None:
    """Extract transmission type from free-text description."""
    # Pass 1: fixed keywords (DSG, PDK, CVT …) — use pre-mapped canonical forms
    m = _TX_FIXED.search(text)
    if m:
        canonical = _TX_FIXED_MAP.get(m.group(0).lower())
        if canonical:
            return canonical
        result = normalize_transmission(m.group(0))
        if result:
            return result

    # Pass 2: structured patterns sentence-by-sentence
    for sent in _sentences(text):
        m = _TX_PATTERN.search(sent)
        if not m:
            continue
        snippet = m.group(0).strip().strip(",")
        if 3 < len(snippet) < 70:
            result = normalize_transmission(snippet)
            if result:
                return result
            # normalize_transmission didn't resolve (e.g. "6 speed" without qualifier)
            # Fall through to try a wider context window in Pass 3

    # Pass 3: generic keyword in context — grab surrounding phrase for richer input
    for sent in _sentences(text):
        m = _TRANS_KW_RE.search(sent)
        if not m:
            continue
        start = max(0, m.start())
        end = min(len(sent), m.end() + 25)
        snippet = sent[start:end].strip()
        snippet = re.split(r"[,;]", snippet)[0].strip()
        if 3 < len(snippet) < 60:
            result = normalize_transmission(snippet)
            if result:
                return result
    return None


def parse_vin_from_listing_html(html: str | None) -> str | None:
    """Extract VIN from Carbuckle's Vue-rendered spec table (``div.specsColumn``)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for col in soup.find_all("div", class_=lambda c: c and "specsColumn" in (c or "")):
        name_el = col.find("div", class_=lambda c: c and "specsFieldName" in (c or ""))
        val_el = col.find("div", class_=lambda c: c and "specsFieldValue" in (c or ""))
        if not name_el or not val_el:
            continue
        label = name_el.get_text(strip=True).lower().replace("#", "").replace(" ", "")
        if "vin" not in label:
            continue
        # Prefer ``title`` attribute (has the clean value even when text is icon-polluted)
        span = val_el.find("span", class_=lambda c: c and "specsValueText" in (c or ""))
        raw = (span.get("title") or span.get_text(strip=True)) if span else val_el.get_text(strip=True)
        raw = raw.strip()
        result = _validate_vin_string(raw)
        if result:
            return result
    return None


def merge_firestore_into_row(row: dict[str, Any], fs_doc: dict) -> dict[str, Any]:
    """Enrich a parsed row with data from a Firestore document.

    Updates the row *in place* and returns it.
    Fields already populated in the row are not overwritten.
    """
    flat = flatten_firestore_doc(fs_doc)

    # Description: prefer fullDesc > cmsContent; augment if row has only the fallback stub
    cms_has_real_content = bool(row.get("listing_description") and
                                len(row.get("listing_description", "")) > 120)
    for desc_key in ("fullDesc", "cmsContent"):
        fs_desc = (flat.get(desc_key) or "").strip()
        if fs_desc and not cms_has_real_content:
            features = (flat.get("features") or "").strip()
            if features:
                row["listing_description"] = f"Highlights: {features}\n\n{fs_desc}"
            else:
                row["listing_description"] = fs_desc
            break

    # Facet spec fields — only fill gaps
    if not row.get("ext_color"):
        row["ext_color"] = (flat.get("facet_exteriorColor") or "").strip() or None
    if not row.get("int_color"):
        row["int_color"] = (flat.get("facet_interiorColor") or "").strip() or None
    if not row.get("engine_spec"):
        raw_engine = (flat.get("facet_motor") or "").strip()
        row["engine_spec"] = raw_engine or None
    if not row.get("transmission"):
        raw_tx = (flat.get("facet_transmission") or "").strip()
        if raw_tx:
            row["transmission"] = normalize_transmission(raw_tx)
        else:
            # facet_isAutomatic is set when the seller ticks "Automatic: Yes/No"
            is_auto = flat.get("facet_isAutomatic")
            if is_auto is None:
                is_auto = flat.get("isAutomatic")
            if is_auto is True:
                row["transmission"] = "automatic"
            elif is_auto is False:
                row["transmission"] = "manual"
    if not row.get("vin"):
        row["vin"] = parse_vin(flat)

    # Price — reuse same rules as Typesense (OFFER uses lastBidValue when priceDisplay is 0)
    if row.get("amount") is None:
        amt_fs, _ = parse_price(flat)
        if amt_fs is not None:
            row["amount"] = amt_fs

    # Sold date — Firestore exposes lastBidTime as ISO; Typesense may only have ms fields
    sd = parse_selling_date(flat)
    if sd:
        row["selling_date"] = sd

    # Country / location — fill from Firestore when Typesense omitted them
    if not row.get("country"):
        fs_country = (flat.get("country") or "").strip() or None
        if fs_country:
            row["country"] = fs_country
    if not row.get("location"):
        fs_label = (flat.get("locationLabel") or "").strip() or None
        fs_city = (flat.get("city") or "").strip()
        fs_state = (flat.get("state") or "").strip()
        fs_ctry = (flat.get("country") or "").strip()
        if fs_label:
            row["location"] = fs_label
        elif any((fs_city, fs_state, fs_ctry)):
            row["location"] = ", ".join(p for p in (fs_city, fs_state, fs_ctry) if p)

    # If engine/transmission still missing, try to extract from description text
    desc_text = row.get("listing_description") or ""
    if not row.get("engine_spec") and desc_text:
        row["engine_spec"] = _extract_engine_from_text(desc_text)
    if not row.get("transmission") and desc_text:
        row["transmission"] = _extract_transmission_from_text(desc_text)

    return row
