"""Parse Typesense documents and listing HTML (``data-auction-*``, description)."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import KM_TO_MILES, country_code_to_name, normalize_transmission, normalize_vin

from .config import LISTING_BASE

_NON_VEHICLE_TITLE_RE = re.compile(
    r"\b(?:go[- ]?kart|karting|half[- ]scale|scale\s+model|model\s+car|"
    r"painting|artwork|sculpture|poster|lithograph|memorabilia|"
    r"diecast|trophy|signage|luggage|briefcase|suitcase)\b",
    re.I,
)


def _parse_year(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})", s)
    if m:
        try:
            y = int(m.group(1))
            return y if 1885 <= y <= 2100 else None
        except ValueError:
            return None
    return None


def _currency_iso(code: str | None) -> str:
    if not code:
        return "USD"
    c = str(code).strip().lower()
    if len(c) == 3 and c.isalpha():
        return c.upper()
    aliases = {
        "eur": "EUR",
        "gbp": "GBP",
        "usd": "USD",
        "aud": "AUD",
        "nzd": "NZD",
        "cad": "CAD",
        "chf": "CHF",
        "jpy": "JPY",
    }
    return aliases.get(c, c.upper() if len(c) == 3 else "USD")


def _mileage_mi(raw: str | None) -> int | None:
    """Collecting Cars strings like ``32,000 Km`` — delegate to the shared
    normalizer (now unit-aware) and default to km when the unit is absent
    since CC's catalogue is UK/EU-first.
    """
    if not raw:
        return None
    from shared.normalizer import normalize_mileage

    low = str(raw).lower()
    has_unit = "mile" in low or re.search(r"\bmi\b", low) or "km" in low or "kilomet" in low
    return normalize_mileage(str(raw), unit=None if has_unit else "km")


def _engine(powertrain: str | None, fuel: str | None) -> str | None:
    parts = [p for p in (powertrain, fuel) if p]
    return " — ".join(parts) if parts else None


def _selling_date(dt: str | None) -> str | None:
    if not dt:
        return None
    s = str(dt).strip()
    return s[:10] if len(s) >= 10 else None


def parse_typesense_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Typesense hit ``document`` into a summary row.

    Returns ``None`` for non-vehicle lots (trophies, scale models, artwork…).
    """
    title = (doc.get("title") or "").strip()
    if _NON_VEHICLE_TITLE_RE.search(title):
        return None

    feats = doc.get("features") or {}
    if not isinstance(feats, dict):
        feats = {}

    mileage_raw = feats.get("mileage")

    slug = doc.get("slug") or ""
    aid = doc.get("auctionId") or doc.get("id")
    record_id = str(aid) if aid is not None else ""

    price = doc.get("priceSold")
    if price is None:
        price = doc.get("currentBid")
    hidden = doc.get("isSoldPriceHidden")
    if hidden is True or str(hidden).lower() in ("1", "true", "yes"):
        price = None

    return {
        "record_id": record_id,
        "slug": slug,
        "title": doc.get("title") or "",
        "year": _parse_year(doc.get("productYear")),
        "make": doc.get("productMake") or doc.get("makeName") or doc.get("vehicleMake"),
        "model": doc.get("modelName") or doc.get("variantName"),
        "vin": None,
        "mileage_mi": _mileage_mi(str(mileage_raw) if mileage_raw is not None else None),
        "ext_color": None,
        "int_color": None,
        "engine_spec": _engine(doc.get("powertrainName"), feats.get("fuelType")),
        "transmission": normalize_transmission(feats.get("transmission")),
        "lot_number": str(aid) if aid is not None else None,
        "currency": _currency_iso(doc.get("currencyCode")),
        "amount_native": int(price) if price is not None else None,
        "selling_date": _selling_date(doc.get("dtSoldUTC")),
        "sold_status": "Sold" if (doc.get("listingStage") or doc.get("stage")) == "sold" else "Unsold",
        "bid_count": doc.get("noBids"),
        "country": country_code_to_name(doc.get("countryCode")),
        "image_url": doc.get("mainImageUrl"),
        "listing_url": f"{LISTING_BASE}/{slug}" if slug else None,
        "listing_description": None,
    }


def _auction_status_attrs(soup: BeautifulSoup) -> dict[str, str]:
    sec = soup.select_one("#carsAuctionStatus")
    if not sec:
        return {}
    out: dict[str, str] = {}
    for k, v in sec.attrs.items():
        if k.startswith("data-auction-"):
            key = k.replace("data-auction-", "").replace("-", "_")
            out[key] = v if isinstance(v, str) else str(v or "")
    return out


def _clean_description(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if "Show original" in text:
        text = text.split("Show original")[-1].strip()

    # Multilingual blob: keep English block between KEY FACTS and next major heading.
    m = re.search(
        r"KEY FACTS\s*(.+?)\s*(?:DATOS CLAVE|FAITS MARQUANTS|DADOS PRINCIPAIS|VIKTIGA FAKTA|NAJWAŻNIEJSZE|BELANGRIJKSTE|DATI SALIENTI|DADOS PRINCIPAIS|Car Overview)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        text = "KEY FACTS " + m.group(1).strip()

    text = _strip_cc_footer(text)

    return text[:50_000]


# Collecting Cars appends its legal T&Cs + (sometimes) a Spanish import-tax
# disclaimer to every listing. Both are boilerplate — zero non-CC listings
# contain them — so truncating from the first occurrence strips the tail
# cleanly. See P2 §2.2 of the bug report. Known-string match is more robust
# than CSS-selector based strip (a class rename on CC's end would break it).
_CC_FOOTERS = (
    "Descriptions are provided by the seller",
    "Si compras un vehículo en un país distinto al tuyo",
)


def _strip_cc_footer(text: str) -> str:
    """Truncate known CC boilerplate tails — English T&Cs and Spanish
    import-tax disclaimer — wherever they first appear.
    """
    cut = len(text)
    for marker in _CC_FOOTERS:
        idx = text.find(marker)
        if idx > 0 and idx < cut:
            cut = idx
    return text[:cut].rstrip()


# ISO-style VINs (no I/O/Q), 11–17 chars — typical modern road cars.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{11,17}$", re.I)
# Shorter chassis / commission numbers (e.g. AR192575, FD21203316 after stripping punctuation).
_CHASSIS_ALNUM_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{6,17}$", re.I)
# Numeric-only IDs (e.g. data-vin="2520588").
_NUM_CHASSIS_RE = re.compile(r"^\d{4,17}$")


def _normalize_vin(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Site values like ``AM 109 *044*`` or ``FD2-1203316`` — keep alnum only for validation.
    compact = re.sub(r"[\s\-*]+", "", s)
    if not compact:
        return None
    cu = compact.upper()
    if _VIN_RE.match(cu):
        return cu
    if _NUM_CHASSIS_RE.match(compact):
        return compact
    if _CHASSIS_ALNUM_RE.match(cu):
        return cu
    return None


def _parse_overview_list(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Old PDP: ``ul.overviewList`` with icon URLs (mileage, transmission, paint, etc.).
    New PDP: CSS-module rows ``div[class*=__row]`` with label/value spans.
    Tries both structures and merges results.
    """
    out: dict[str, Any] = {}

    # ── Old structure: ul.overviewList with icon <img> tags ──────────────────
    ul = soup.select_one("ul.overviewList")
    if ul:
        for li in ul.find_all("li"):
            img = li.find("img", src=True)
            if not img:
                continue
            src = (img.get("src") or "").lower()
            text = li.get_text(" ", strip=True)
            if not text:
                continue
            if "icon-mileage" in src:
                out["overview_mileage_raw"] = text
                out["mileage_mi"] = _mileage_mi(text)
            elif "icon-transmission" in src:
                out["transmission"] = normalize_transmission(text)
            elif "icon-direction" in src:
                out["drive_side"] = text
            elif "icon-paint" in src:
                out["ext_color"] = text
            elif "icon-interior" in src:
                out["int_color"] = text
            elif "icon-engine" in src or "icon-engin" in src:
                out["engine_overview"] = text
            elif "icon-lot" in src:
                m = re.search(r"#?\s*(\d+)", text)
                if m:
                    out["lot_number"] = m.group(1)

    # ── New structure: CSS-module rows with __label / __value spans ───────────
    # Matches hashed class names like ``listingOverview-module-scss-module__xxx__row``
    for row in soup.select("[class*=__row]"):
        label_el = row.select_one("[class*=__label]")
        value_el = row.select_one("[class*=__value]")
        if not label_el or not value_el:
            continue
        label = label_el.get_text(strip=True).lower()
        value = value_el.get_text(strip=True)
        if not value:
            continue
        if "engine" in label:
            out["engine_overview"] = value
        elif "mileage" in label or "odometer" in label:
            out["overview_mileage_raw"] = value
            out["mileage_mi"] = _mileage_mi(value)
        elif "transmission" in label or "gearbox" in label:
            out["transmission"] = normalize_transmission(value)
        elif "exterior" in label or "colour" in label or "color" in label:
            out["ext_color"] = value
        elif "interior" in label:
            out["int_color"] = value
        elif "lot" in label:
            m = re.search(r"#?\s*(\d+)", value)
            if m:
                out["lot_number"] = m.group(1)

    # ── Lot number from standalone "Lot #XXXXX" text anywhere on the page ─────
    if "lot_number" not in out:
        lot_text = soup.get_text(" ", strip=True)
        m = re.search(r"\bLot\s*#\s*(\d+)\b", lot_text, re.I)
        if m:
            out["lot_number"] = m.group(1)

    return out


_DESC_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.I)
_DESC_CHASSIS_RE = re.compile(
    r"(?:chassis|vin|serial|frame)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{3,23}[A-Z0-9])",
    re.I,
)


def _vin_from_description(text: str) -> str | None:
    """Scan description prose for a VIN or chassis number."""
    if not text:
        return None
    m = _DESC_VIN_RE.search(text)
    if m:
        v = normalize_vin(m.group(1))
        if v:
            return v
    m = _DESC_CHASSIS_RE.search(text)
    if m:
        cand = re.sub(r"[\s\-]", "", m.group(1).strip().upper())
        v = normalize_vin(cand)
        if v:
            return v
    return None


def parse_detail_html(html: str | None) -> dict[str, Any]:
    """Extract auction status, overview list, VIN, and description."""
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    attrs = _auction_status_attrs(soup)
    overview = _parse_overview_list(soup)

    vin_el = soup.select_one("[data-vin]")
    vin = _normalize_vin(vin_el.get("data-vin") if vin_el else None)

    desc_el = soup.select_one(".detailsPage__description")
    description = ""
    if desc_el:
        description = _clean_description(desc_el.get_text(" ", strip=True))

    # Fall back to description scan when data-vin is absent or a placeholder like "TBC".
    if not vin and description:
        vin = _vin_from_description(description)

    price_sold = attrs.get("pricesold") or attrs.get("saleprice")
    amount_native = int(price_sold) if price_sold and str(price_sold).isdigit() else None

    out: dict[str, Any] = {
        "auction_attrs": attrs,
        "listing_description": description or None,
        "amount_native": amount_native,
        "selling_date": _selling_date(attrs.get("date_sold")),
        "make": attrs.get("make"),
        "model": attrs.get("model"),
        "year": _parse_year(attrs.get("modelyear")),
        "currency": _currency_iso(attrs.get("currency_code")),
        "vin": vin,
    }
    for k in ("mileage_mi", "transmission", "ext_color", "int_color", "engine_overview", "drive_side", "lot_number"):
        if overview.get(k):
            out[k] = overview[k]
    return out


def merge_summary(typesense_row: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Prefer Typesense for identity; overlay HTML detail for VIN, colours, and richer specs."""
    out = dict(typesense_row)
    if not detail:
        return out

    if detail.get("listing_description"):
        out["listing_description"] = detail["listing_description"]

    if detail.get("vin"):
        out["vin"] = detail["vin"]

    if detail.get("ext_color"):
        out["ext_color"] = detail["ext_color"]
    if detail.get("int_color"):
        out["int_color"] = detail["int_color"]

    if detail.get("transmission"):
        out["transmission"] = detail["transmission"]

    dm = detail.get("mileage_mi")
    if dm is not None and (out.get("mileage_mi") is None):
        out["mileage_mi"] = dm

    ev = detail.get("engine_overview")
    if ev:
        base = out.get("engine_spec")
        # Prefer the HTML value when it contains a displacement (e.g. "4.5L V8")
        # or when the Typesense value is already contained inside the HTML value.
        _ev_has_displacement = bool(re.search(r"\d+\.?\d*\s*[Ll]\b", ev))
        if not base:
            out["engine_spec"] = ev
        elif _ev_has_displacement or ev.lower() not in base.lower():
            out["engine_spec"] = ev

    for key in ("bid_count", "amount_native", "selling_date", "make", "model", "year", "currency"):
        v = detail.get(key)
        if v is not None and v != "":
            if key in ("make", "model", "year", "currency") and out.get(key):
                continue
            out[key] = v

    if detail.get("lot_number") and not out.get("lot_number"):
        out["lot_number"] = detail["lot_number"]

    return out
