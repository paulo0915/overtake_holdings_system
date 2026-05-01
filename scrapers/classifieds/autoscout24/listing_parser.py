"""Parse AutoScout24 list + detail JSON."""

from __future__ import annotations

import re
from typing import Any

from shared.normalizer import country_code_to_name, normalize_vin

from .config import SITE_ORIGIN

# Listings between 10M and 50M in major units are almost always dealer junk
# unless the make is one that plausibly sells in that range.
_HIGH_VALUE_MAKES = frozenset(
    {
        "bugatti",
        "pagani",
        "koenigsegg",
        "ferrari",
        "lamborghini",
        "mclaren",
        "rolls-royce",
        "aston martin",
        "bentley",
        "rimac",
        "gordon murray",
    }
)


def _make_allows_high_price(make: str | None) -> bool:
    if not make:
        return False
    m = str(make).strip().lower()
    return m in _HIGH_VALUE_MAKES


def _is_placeholder_raw_amount(raw: int, make: str | None) -> bool:
    """Integer amounts that are obviously dealer sentinels (not real list prices)."""
    if raw in (123456789, 12345678, 987654321):
        return True
    s = str(abs(raw))
    if len(s) >= 7 and set(s) == {"9"}:
        return not _make_allows_high_price(make)
    return False


def _is_placeholder_formatted(price_formatted: str | None) -> bool:
    """Dealers often hide real prices behind repeating / joke formatted amounts."""
    if not price_formatted:
        return False
    s = str(price_formatted)
    # Common AS24 placeholders seen in the wild (list + detail both echo them).
    if re.search(r"999[,\s.]?999[,\s.]?999", s):
        return True
    if re.search(r"123[,\s.]?456[,\s.]?789", s):
        return True
    if re.search(r"295[,\s.]?000[,\s.]?000", s):  # fake €295M pattern
        return True
    return False


def _digits_from_formatted(price_formatted: str | None) -> int | None:
    """Strip currency symbols / thousands separators → integer major units."""
    if not price_formatted:
        return None
    digits = re.sub(r"[^\d]", "", str(price_formatted))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _parse_price_currency(
    price_formatted: str | None,
    tracking: dict | None,
    *,
    make: str | None = None,
) -> tuple[int | None, str]:
    """Return (amount in **major** currency units, ISO currency code).

    Prefer ``tracking.price`` when it is in a sane range — it matches the
    formatted card price in the happy path.  Strip-all-digits from
    ``priceFormatted`` breaks DE-style amounts (``€ 34.520`` vs ``34.520,00``).

    Ignore obvious placeholders (``999999999``) on either field.
    """
    pf = (price_formatted or "").strip()
    cur = "EUR"
    if pf.startswith("€") or pf.startswith("\u20ac"):
        cur = "EUR"
    elif pf.startswith("£"):
        cur = "GBP"
    elif pf.upper().startswith("CHF"):
        cur = "CHF"
    elif pf.startswith("$") or pf.startswith("US$"):
        cur = "USD"

    if _is_placeholder_formatted(pf):
        return None, cur

    t_p: int | None = None
    if tracking and tracking.get("price") is not None:
        try:
            t_p = int(str(tracking["price"]).replace(",", "").strip())
        except (TypeError, ValueError):
            t_p = None
    if t_p is not None and t_p >= 999_000_000:
        t_p = None

    raw: int | None = None
    if t_p is not None and 0 < t_p < 99_000_000:
        raw = t_p
    else:
        raw = _digits_from_formatted(pf)
        if raw is not None and raw >= 999_000_000:
            raw = None

    # Residual nonsense (e.g. ``23500000`` for a Nissan Juke with no commas in tracking).
    if raw is not None and raw >= 50_000_000:
        raw = None

    if raw is not None and _is_placeholder_raw_amount(raw, make):
        raw = None

    if raw is not None and 10_000_000 <= raw < 50_000_000 and not _make_allows_high_price(make):
        raw = None

    return raw, cur


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
    amount, currency = _parse_price_currency(
        price_block.get("priceFormatted"),
        tracking,
        make=veh.get("make"),
    )

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
    engine_bits = [veh.get("cylinderCapacity"), veh.get("powerInHp"), veh.get("fuelCategory", {}).get("formatted") if isinstance(veh.get("fuelCategory"), dict) else veh.get("primaryFuel")]
    engine_spec = " · ".join(str(x) for x in engine_bits if x) or None

    trans = veh.get("transmissionType")
    desc = (detail.get("description") or "").strip() or None

    loc = detail.get("location") or {}
    country = country_code_to_name(loc.get("countryCode"))

    seller = detail.get("seller") or {}
    dealer = (seller.get("companyName") or "").strip() or None

    pub = (detail.get("prices") or {}).get("public") or {}
    top_price = detail.get("price") or {}
    d_tracking = detail.get("tracking") if isinstance(detail.get("tracking"), dict) else None
    pf_detail: str | None = None
    if isinstance(pub.get("price"), str) and pub.get("price").strip():
        pf_detail = pub["price"].strip()
    elif top_price.get("priceFormatted"):
        pf_detail = str(top_price["priceFormatted"]).strip()
    tr_for_price = d_tracking
    if pub.get("priceRaw") is not None:
        try:
            prw = int(pub["priceRaw"])
            if prw > 0:
                tr_for_price = {**(tr_for_price or {}), "price": prw}
        except (TypeError, ValueError):
            pass
    d_make = (veh.get("make") or "").strip() or None
    d_price, d_cur = (None, "EUR")
    if pub.get("onRequestOnly") is True and pub.get("priceRaw") in (None, 0):
        d_price, d_cur = None, "EUR"
    elif pf_detail or tr_for_price:
        d_price, d_cur = _parse_price_currency(pf_detail, tr_for_price, make=d_make)

    out: dict[str, Any] = {
        "vin": vin,
        "year": year,
        "mileage_mi": mileage_mi,
        "ext_color": ext,
        "int_color": None,
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
