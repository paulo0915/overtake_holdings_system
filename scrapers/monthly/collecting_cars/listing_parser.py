"""Parse Typesense documents and listing HTML (``data-auction-*``, description)."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import KM_TO_MILES, country_code_to_name

from .config import LISTING_BASE


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
    """Collecting Cars shows ``Km`` in strings; avoid ``normalize_mileage``'s ``k`` suffix trap."""
    if not raw:
        return None
    m = re.search(r"([\d,]+\.?\d*)", str(raw))
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    low = str(raw).lower()
    if "mile" in low or re.search(r"\bmi\b", low):
        return round(val)
    if "km" in low or "kilomet" in low:
        return round(val * KM_TO_MILES)
    return round(val * KM_TO_MILES)


def _engine(powertrain: str | None, fuel: str | None) -> str | None:
    parts = [p for p in (powertrain, fuel) if p]
    return " — ".join(parts) if parts else None


def _selling_date(dt: str | None) -> str | None:
    if not dt:
        return None
    s = str(dt).strip()
    return s[:10] if len(s) >= 10 else None


def parse_typesense_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Typesense hit ``document`` into a summary row."""
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
        "transmission": feats.get("transmission"),
        "lot_number": record_id or None,
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

    return text[:50_000]


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
    Car PDP uses ``ul.overviewList`` with icon URLs (mileage, transmission, paint, etc.).
    """
    out: dict[str, Any] = {}
    ul = soup.select_one("ul.overviewList")
    if not ul:
        return out

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
            out["transmission"] = text
        elif "icon-direction" in src:
            out["drive_side"] = text
        elif "icon-paint" in src:
            out["ext_color"] = text
        elif "icon-interior" in src:
            out["int_color"] = text
        elif "icon-engine" in src or "icon-engin" in src:
            out["engine_overview"] = text
    return out


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

    bids = attrs.get("bids")
    bid_count = int(bids) if bids and str(bids).isdigit() else None

    price_sold = attrs.get("pricesold") or attrs.get("saleprice")
    amount_native = int(price_sold) if price_sold and str(price_sold).isdigit() else None

    out: dict[str, Any] = {
        "auction_attrs": attrs,
        "listing_description": description or None,
        "bid_count": bid_count,
        "amount_native": amount_native,
        "selling_date": _selling_date(attrs.get("date_sold")),
        "make": attrs.get("make"),
        "model": attrs.get("model"),
        "year": _parse_year(attrs.get("modelyear")),
        "currency": _currency_iso(attrs.get("currency_code")),
        "vin": vin,
    }
    for k in ("mileage_mi", "transmission", "ext_color", "int_color", "engine_overview", "drive_side"):
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
        if base and ev.lower() not in base.lower():
            out["engine_spec"] = f"{base} — {ev}"
        elif not base:
            out["engine_spec"] = ev

    for key in ("bid_count", "amount_native", "selling_date", "make", "model", "year", "currency"):
        v = detail.get(key)
        if v is not None and v != "":
            if key in ("make", "model", "year", "currency") and out.get(key):
                continue
            out[key] = v

    return out
