"""RM Sotheby's — results-page auction discovery, SearchLots rows, lot HTML."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from .config import SITE_ORIGIN

# Only match the lots-index URL ``/auctions/{slug}/lots/`` — NOT deep lot URLs
# like ``/auctions/{slug}/lots/r0001-…``. Sealed / single-lot auctions that link
# directly to the lot are intentionally skipped (they don't expose a SearchLots index).
_AUCTION_LOTS_HREF_RE = re.compile(
    r"https?://(?:www\.)?rmsothebys\.com/auctions/([a-z0-9]+)/lots/?(?=[\"'?#]|\s|$)",
    re.I,
)
_AUCTION_LOTS_PATH_RE = re.compile(r'["\']/auctions/([a-z0-9]+)/lots/?["\']', re.I)
_AUCTION_LOTS_REL_RE = re.compile(r"/auctions/([a-z0-9]+)/lots/?(?=[\"'?#]|\s|$)", re.I)

# Vehicle lots use ``referenceId`` like ``r0048``; ``n1000`` = memorabilia / parts.
_VEHICLE_REF_RE = re.compile(r"^r\d+", re.I)

_YEAR_LEADING_RE = re.compile(
    r"^(?P<year>(?:19|20)\d{2})\s+(?P<rest>.+)$",
)

# Monetary tokens in ``value`` / ``preSaleEstimate`` strings.
_NUM = r"[\d,]+(?:\.\d+)?"
_MONEY_RE = re.compile(
    rf"(?P<a>{_NUM})\s*(?P<cur>USD|EUR|GBP|CHF)\b"
    rf"|(?P<sym>£|\$|€)\s*(?P<b>{_NUM})\s*(?P<cur2>USD|EUR|GBP|CHF)?"
    rf"|\bCHF\s*(?P<c>{_NUM})"
    rf"|\b(?P<d>{_NUM})\s*(?P<e>USD|EUR|GBP|CHF)\b",
    re.I,
)


def parse_auction_slugs_from_results_html(html: str) -> list[str]:
    """Return unique auction slugs (lowercase) found on a ``/results/{year}`` page."""
    found: set[str] = set()
    for rx in (_AUCTION_LOTS_HREF_RE, _AUCTION_LOTS_PATH_RE, _AUCTION_LOTS_REL_RE):
        for m in rx.finditer(html):
            slug = m.group(1).lower()
            if slug:
                found.add(slug)
    return sorted(found)


# ``8 July 2025`` / ``16–17 August 2025`` / ``August 16-17, 2025`` — capture last day.
_DATE_DMY_RE = re.compile(
    r"(?P<d1>\d{1,2})(?:\s*[–—-]\s*(?P<d2>\d{1,2}))?\s+"
    r"(?P<mon>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<year>20\d{2})",
    re.I,
)
_DATE_MDY_RE = re.compile(
    r"(?P<mon>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<d1>\d{1,2})(?:\s*[–—-]\s*(?P<d2>\d{1,2}))?,?\s+(?P<year>20\d{2})",
    re.I,
)


def _parse_auction_date(subtitle: str) -> str | None:
    """``8 July 2025`` / ``16–17 August 2025`` / ``August 16-17, 2025`` → ``YYYY-MM-DD`` (last day)."""
    if not subtitle:
        return None
    s = subtitle.replace("\u2013", "-").replace("\u2014", "-")
    for rx in (_DATE_DMY_RE, _DATE_MDY_RE):
        m = rx.search(s)
        if not m:
            continue
        day = int(m.group("d2") or m.group("d1"))
        mon = m.group("mon")
        year = int(m.group("year"))
        try:
            return datetime.strptime(f"{day} {mon} {year}", "%d %B %Y").date().isoformat()
        except ValueError:
            return None
    return None


def parse_auctions_from_results_html(html: str) -> list[dict[str, Any]]:
    """Return ``[{slug, name, date}]`` per auction card on ``/results/{year}``."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for section in soup.select("section.rms-section"):
        a = section.find("a", href=_AUCTION_LOTS_REL_RE)
        href = ""
        if a and a.get("href"):
            href = a["href"]
        else:
            item = section.find(attrs={"href": _AUCTION_LOTS_REL_RE})
            href = item.get("href", "") if item else ""
        m = _AUCTION_LOTS_REL_RE.search(href)
        if not m:
            continue
        slug = m.group(1).lower()
        if slug in seen:
            continue
        seen.add(slug)

        name_el = section.select_one(".tile__title")
        name = name_el.get_text(" ", strip=True) if name_el else None
        sub_el = section.select_one(".tile__subtitle")
        sub = sub_el.get_text(" ", strip=True) if sub_el else ""
        date = _parse_auction_date(sub)

        out.append({"slug": slug, "name": name, "date": date})

    # Fallback: regex-only discovery for slugs without a recognizable card.
    regex_slugs = set(parse_auction_slugs_from_results_html(html))
    for s in sorted(regex_slugs - seen):
        out.append({"slug": s, "name": None, "date": None})

    return out


def is_vehicle_lot(item: dict[str, Any]) -> bool:
    """True for road-car rows (``referenceId`` ``r…``), excluding parts/memorabilia (``n…``)."""
    ref = str(item.get("referenceId") or "").strip()
    link = str(item.get("link") or "")
    if not _VEHICLE_REF_RE.match(ref):
        return False
    return f"/lots/{ref.lower()}-" in link.replace("_", "-").lower() or f"/lots/{ref}-" in link


def _parse_amount(num: str) -> int:
    return int(float(num.replace(",", "")))


def extract_money_amounts(raw: str) -> list[tuple[int, str]]:
    """Return ``(amount, ISO_currency)`` tuples in left-to-right order."""
    if not raw or not raw.strip():
        return []
    s = raw.replace("\u2013", "-").replace("\u2014", "-")
    out: list[tuple[int, str]] = []
    for m in _MONEY_RE.finditer(s):
        cur = (m.group("cur") or m.group("cur2") or m.group("e") or "").upper()
        if m.group("a") and m.group("cur"):
            amt, cur = m.group("a"), m.group("cur").upper()
        elif m.group("sym") and m.group("b"):
            sym = m.group("sym")
            amt = m.group("b")
            cur = (m.group("cur2") or "").upper()
            if not cur:
                cur = {"£": "GBP", "$": "USD", "€": "EUR"}.get(sym, "USD")
        elif m.group("c"):
            amt, cur = m.group("c"), "CHF"
        elif m.group("d") and m.group("e"):
            amt, cur = m.group("d"), m.group("e").upper()
        else:
            continue
        try:
            out.append((_parse_amount(amt), cur))
        except ValueError:
            continue
    return out


def parse_year_make_model_from_title(title: str) -> tuple[int | None, str | None, str | None]:
    title = html_lib.unescape((title or "").strip())
    m = _YEAR_LEADING_RE.match(title)
    if not m:
        return None, None, title or None
    year = int(m.group("year"))
    rest = m.group("rest").strip()
    parts = rest.split(None, 1)
    if len(parts) == 2:
        return year, parts[0], parts[1]
    if len(parts) == 1:
        return year, parts[0], None
    return year, None, None


def parse_value_fields(
    value: str | None,
    value_type: str | None,
    sold_flag: bool | None,
    pre_sale_estimate: str | None,
) -> dict[str, Any]:
    """Derive sold status, hammer / estimates, and currency from API strings."""
    vt = (value_type or "").strip()
    sold = bool(sold_flag) or (vt.lower() == "sold")

    amounts_main = extract_money_amounts(value or "")
    amounts_pre = extract_money_amounts(pre_sale_estimate or "")

    selling_price: int | None = None
    bid_price: int | None = None
    estimate_low: int | None = None
    estimate_high: int | None = None
    currency: str | None = None

    if sold:
        if len(amounts_main) >= 1:
            selling_price = amounts_main[0][0]
            currency = amounts_main[0][1]
        elif len(amounts_main) >= 2:
            selling_price = amounts_main[-1][0]
            currency = amounts_main[-1][1]
    else:
        if len(amounts_main) >= 2 and amounts_main[0][1] == amounts_main[1][1]:
            estimate_low, estimate_high = amounts_main[0][0], amounts_main[1][0]
            currency = amounts_main[0][1]
        elif len(amounts_main) == 1:
            bid_price = amounts_main[0][0]
            currency = amounts_main[0][1]
        if estimate_low is None and amounts_pre:
            if len(amounts_pre) >= 2 and amounts_pre[0][1] == amounts_pre[1][1]:
                estimate_low, estimate_high = amounts_pre[0][0], amounts_pre[1][0]
                currency = currency or amounts_pre[0][1]
            elif len(amounts_pre) == 1:
                estimate_low = amounts_pre[0][0]
                currency = currency or amounts_pre[0][1]

    if currency is None and amounts_main:
        currency = amounts_main[0][1]
    if currency is None and amounts_pre:
        currency = amounts_pre[0][1]

    if sold:
        status = "Sold"
    elif vt.lower() in {"not sold", "unsold"}:
        status = "Unsold"
    elif vt:
        status = vt
    else:
        status = "Unsold"

    has_estimate = estimate_low is not None or estimate_high is not None
    return {
        "sold": sold,
        "sold_status_label": status,
        "selling_price": selling_price,
        "bid_price": bid_price,
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "estimate_currency": currency if has_estimate else None,
        "currency": currency or "USD",
    }


def parse_lot_detail_html(html: str) -> dict[str, Any]:
    """Parse ``idlabel`` / ``iddata`` blocks + ``og:title`` from a lot detail page."""
    soup = BeautifulSoup(html, "html.parser")
    pairs: dict[str, str] = {}
    for lbl in soup.select("div.idlabel"):
        label = lbl.get_text(" ", strip=True)
        parent = lbl.find_parent("div", class_=lambda c: c and "col-lg" in str(c))
        if not parent:
            continue
        data_el = parent.select_one("div.iddata")
        if not data_el:
            continue
        val = data_el.get_text(" ", strip=True)
        val = re.sub(r"^\|\s*", "", val).strip()
        if label and val and label not in pairs:
            pairs[label] = val

    vin = pairs.get("VIN") or pairs.get("Chassis No.") or pairs.get("Chassis Number")
    mileage = pairs.get("Mileage") or pairs.get("Odometer") or pairs.get("Indicated Mileage")
    ext = pairs.get("Exterior Color") or pairs.get("Colour") or pairs.get("Color")
    intc = pairs.get("Interior Color") or pairs.get("Interior")
    # RM exposes ``Engine No.`` (serial) as a labeled field — not a true engine spec.
    # Only populate Engine_spec when an actual spec label (rare) is present.
    engine = pairs.get("Engine")
    trans = pairs.get("Transmission")
    location = pairs.get("Location")

    og = soup.find("meta", attrs={"property": "og:title"})
    og_title = og.get("content") if og and og.get("content") else None

    desc_el = soup.select_one("section.lotdescription") or soup.select_one(
        'section[class*="lotdescription"]',
    )
    description = desc_el.get_text(" ", strip=True) if desc_el else None

    img = soup.find("meta", attrs={"property": "og:image"})
    image_url = None
    if img and img.get("content"):
        image_url = img["content"].strip()
        if image_url.startswith("/"):
            image_url = SITE_ORIGIN + image_url

    return {
        "vin": vin,
        "mileage_raw": mileage,
        "ext_color": ext,
        "int_color": intc,
        "engine_spec": engine,
        "transmission": trans,
        "location": location,
        "description": description,
        "og_title": og_title,
        "image_url": image_url,
        "detail_pairs": pairs,
    }


def parse_mileage_mi(raw: str | None) -> int | None:
    if not raw:
        return None
    s = raw.lower().replace(",", "")
    # "14,250 km" / "8,500 miles" / "8500 mi"
    m = re.search(r"([\d.]+)\s*(km|mi|mile|miles)\b", s)
    if not m:
        digits = re.search(r"([\d,]+)\s*$", raw)
        if not digits:
            return None
        try:
            return int(digits.group(1).replace(",", ""))
        except ValueError:
            return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "km":
        return int(round(val * 0.621371))
    return int(round(val))


def merge_api_item_with_detail(
    *,
    auction_slug: str,
    auction_code_upper: str,
    item: dict[str, Any],
    detail: dict[str, Any] | None,
    auction_name: str | None = None,
    auction_date: str | None = None,
) -> dict[str, Any]:
    """Single merged row for ``record_builder``."""
    link = str(item.get("link") or "")
    listing_url = f"{SITE_ORIGIN}{link}" if link.startswith("/") else f"{SITE_ORIGIN}/{link}"

    public_name = html_lib.unescape(str(item.get("publicName") or "").strip())
    year, make, model = parse_year_make_model_from_title(public_name)

    vf = parse_value_fields(
        str(item.get("value") or ""),
        str(item.get("valueType") or ""),
        item.get("sold"),
        str(item.get("preSaleEstimate") or ""),
    )

    ref = str(item.get("referenceId") or "").strip()
    lot_uuid = str(item.get("id") or "").strip()
    record_id = f"rm_{auction_slug.lower()}_{ref or lot_uuid}"

    crop = str(item.get("crop") or "").strip()
    thumb = crop if crop.startswith("http") else (SITE_ORIGIN + crop if crop.startswith("/") else crop or None)

    d = detail or {}
    image_url = d.get("image_url") or thumb

    return {
        "record_id": record_id,
        "auction_slug": auction_slug.lower(),
        "auction_code_upper": auction_code_upper,
        "auction_name": auction_name,
        "auction_date": auction_date,
        "lot_uuid": lot_uuid or None,
        "reference_id": ref or None,
        "lot_number": str(item.get("lot") or "").strip() or None,
        "header": str(item.get("header") or "").strip() or None,
        "title": public_name,
        "year": year,
        "make": make,
        "model": model,
        "listing_url": listing_url,
        "image_url": image_url,
        "crop_url": thumb,
        "value_raw": str(item.get("value") or ""),
        "value_type_raw": str(item.get("valueType") or ""),
        **vf,
        "vin": d.get("vin"),
        "mileage_mi": parse_mileage_mi(d.get("mileage_raw")),
        "ext_color": d.get("ext_color"),
        "int_color": d.get("int_color"),
        "engine_spec": d.get("engine_spec"),
        "transmission": d.get("transmission"),
        "location": d.get("location"),
        "description": d.get("description"),
        "og_title": d.get("og_title"),
    }
