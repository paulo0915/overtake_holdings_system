"""Parse AutoScout24 list + detail JSON."""

from __future__ import annotations

import re
from typing import Any

from shared.normalizer import country_code_to_name, normalize_vin

from .config import SITE_ORIGIN

def _detect_currency(price_formatted: str | None) -> str:
    """Detect ISO currency code from a formatted price string."""
    pf = (price_formatted or "").strip()
    if pf.startswith("€") or pf.startswith("\u20ac"):
        return "EUR"
    if pf.startswith("£"):
        return "GBP"
    if pf.upper().startswith("CHF"):
        return "CHF"
    if pf.startswith("$") or pf.upper().startswith("US$"):
        return "USD"
    return "EUR"


def _km_to_mi(km: int | None) -> int | None:
    if km is None:
        return None
    return int(round(km * 0.621371))


def _parse_km_string(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"([\d\s.,]+)\s*km", str(s), re.I)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def parse_list_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """One SRP listing object → flat row for detail merge."""
    if not item or not isinstance(item, dict):
        return None
    vid = str(item.get("id") or "").strip()
    path = (item.get("url") or "").strip()
    if not vid or not path.startswith("/offers/"):
        return None
    veh = item.get("vehicle") or {}
    if (veh.get("articleType") or veh.get("type") or "").lower() != "car":
        return None

    price_block = item.get("price") or {}
    tracking = item.get("tracking") or {}
    pf = price_block.get("priceFormatted")
    currency = _detect_currency(pf)
    t_price = tracking.get("price") if isinstance(tracking, dict) else None
    amount: int | None = None
    if t_price is not None:
        try:
            v = int(str(t_price).replace(",", "").strip())
            if v > 0:
                amount = v
        except (TypeError, ValueError):
            pass

    loc = item.get("location") or {}
    country = country_code_to_name(loc.get("countryCode"))

    title_parts = [veh.get("make"), veh.get("model"), veh.get("modelVersionInput")]
    title = " ".join(p for p in title_parts if p and str(p).strip()).strip() or None

    year = None
    fr = tracking.get("firstRegistration") if isinstance(tracking, dict) else None
    if fr and str(fr).lower() != "new":
        m = re.match(r"(\d{2})-(\d{4})", str(fr))
        if m:
            year = int(m.group(2))
        else:
            m2 = re.search(r"(19|20)\d{2}", str(fr))
            if m2:
                year = int(m2.group(0))

    return {
        "record_id": f"as24_{vid}",
        "listing_id": vid,
        "listing_url": f"{SITE_ORIGIN}{path}",
        "offer_path": path,
        "title": title,
        "year": year,
        "make": veh.get("make"),
        "model": veh.get("model") or veh.get("modelGroup"),
        "image_url": (item.get("images") or [None])[0],
        "asking_price": amount,
        "currency": currency,
        "mileage_km_hint": _parse_km_string(veh.get("mileageInKm")),
        "transmission_hint": veh.get("transmission"),
        "fuel_hint": veh.get("fuel"),
        "offer_type": (veh.get("offerType") or "").upper(),  # U / N
        "country": country,
        "city": (loc.get("city") or "").strip() or None,
        "dealer_name": ((item.get("seller") or {}).get("companyName") or "").strip() or None,
    }


def parse_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """``listingDetails`` JSON → supplemental fields."""
    if not detail:
        return {}
    veh = detail.get("vehicle") or {}
    ident = veh.get("identifier") or {}
    vin = ident.get("vin") if isinstance(ident, dict) else None
    if vin:
        vin = normalize_vin(str(vin).strip()) or str(vin).strip().upper()

    mileage_mi = None
    raw_km = veh.get("mileageInKmRaw")
    if raw_km is not None:
        try:
            mileage_mi = _km_to_mi(int(raw_km))
        except (TypeError, ValueError):
            mileage_mi = _km_to_mi(_parse_km_string(veh.get("mileageInKm")))

    year = None
    fr = veh.get("firstRegistrationDateRaw") or veh.get("firstRegistrationDate")
    if fr and str(fr).lower() != "new":
        m = re.search(r"(19|20)\d{2}", str(fr))
        if m:
            year = int(m.group(0))

    ext = veh.get("bodyColor") or veh.get("bodyColorRaw")

    int_clr_raw = veh.get("upholsteryColor")
    int_color = (str(int_clr_raw).strip() or None) if int_clr_raw else None

    fc = veh.get("fuelCategory") or {}
    pf = veh.get("primaryFuel") or {}
    fuel_str = (
        (fc.get("formatted") if isinstance(fc, dict) else None)
        or (pf.get("formatted") if isinstance(pf, dict) else None)
    )
    engine_bits = [veh.get("cylinderCapacity"), veh.get("powerInHp"), fuel_str]
    engine_spec = " · ".join(str(x) for x in engine_bits if x) or None

    trans = veh.get("transmissionType")
    desc = (detail.get("description") or "").strip() or None

    loc = detail.get("location") or {}
    country = country_code_to_name(loc.get("countryCode"))

    seller = detail.get("seller") or {}
    dealer = (seller.get("companyName") or "").strip() or None

    pub = (detail.get("prices") or {}).get("public") or {}
    top_price = detail.get("price") or {}

    pf_str: str | None = None
    if isinstance(pub.get("price"), str) and pub["price"].strip():
        pf_str = pub["price"].strip()
    elif top_price.get("priceFormatted"):
        pf_str = str(top_price["priceFormatted"]).strip()

    d_cur = _detect_currency(pf_str)
    d_price: int | None = None

    prw = pub.get("priceRaw")
    if prw is not None:
        try:
            v = int(prw)
            if v > 0:
                d_price = v
        except (TypeError, ValueError):
            pass

    out: dict[str, Any] = {
        "vin": vin,
        "year": year,
        "mileage_mi": mileage_mi,
        "ext_color": ext,
        "int_color": int_color,
        "engine_spec": engine_spec,
        "transmission": trans,
        "listing_description": desc,
        "country": country,
        "dealer_name": dealer,
    }
    if pub or top_price.get("priceFormatted"):
        out["asking_price"] = d_price
        out["currency"] = d_cur
    return out
