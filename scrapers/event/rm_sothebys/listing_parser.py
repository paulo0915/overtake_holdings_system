"""RM Sotheby's — results-page auction discovery, SearchLots rows, lot HTML."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import extract_description_fields, normalize_transmission

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
    r"^(?:c\.?\s*|circa\s*)?(?P<year>(?:19|20)\d{2})\s+(?P<rest>.+)$",
    re.I,
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

        # Append year to name when the tile title omits it (e.g. "The Monaco Auction" → "The Monaco Auction 2026").
        if name and date and len(date) >= 4 and not re.search(r"\b20\d{2}\b", name):
            name = f"{name} {date[:4]}"
        out.append({"slug": slug, "name": name, "date": date})

    # Fallback: regex-only discovery for slugs without a recognizable card.
    regex_slugs = set(parse_auction_slugs_from_results_html(html))
    for s in sorted(regex_slugs - seen):
        out.append({"slug": s, "name": None, "date": None})

    return out


def parse_upcoming_slugs_from_html(html: str) -> list[str]:
    """Unique lowercase slugs from ``/upcoming/`` — follows the ``VIEW ALL LOTS`` buttons."""
    found: set[str] = set()
    for m in _AUCTION_LOTS_REL_RE.finditer(html):
        slug = (m.group(1) or "").lower()
        if slug:
            found.add(slug)
    return sorted(found)


def parse_auction_meta_from_lots_page(html: str) -> dict[str, Any]:
    """From a ``/auctions/{slug}/lots/`` landing page — ``{name, date}``.

    The ``<title>`` has shape ``The Monaco Auction 2026 | Available Lots | RM Sotheby's`` and
    the page body contains ``25 April 2026`` style dates.
    """
    soup = BeautifulSoup(html, "html.parser")
    name: str | None = None
    og = soup.find("meta", attrs={"property": "og:title"}) or soup.find(
        "meta", attrs={"name": "og:title"},
    )
    if og and og.get("content"):
        name = og["content"].split("|")[0].strip() or None
    if not name:
        t = soup.find("title")
        if t:
            name = t.get_text(" ", strip=True).split("|")[0].strip() or None

    date = _parse_auction_date(html_lib.unescape(html))
    return {"name": name, "date": date}


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


_ESTIMATE_LOW_ONLY_RE = re.compile(r"\b(?:in\s+excess\s+of|starting\s+at|from)\b", re.I)


def parse_value_fields(
    value: str | None,
    value_type: str | None,
    sold_flag: bool | None,
    pre_sale_estimate: str | None,
) -> dict[str, Any]:
    """Derive sold status, hammer / estimates, and currency from API strings."""
    vt = (value_type or "").strip()
    sold = bool(sold_flag) or (vt.lower() == "sold")

    value_str = value or ""
    amounts_main = extract_money_amounts(value_str)
    amounts_pre = extract_money_amounts(pre_sale_estimate or "")
    low_only = bool(_ESTIMATE_LOW_ONLY_RE.search(value_str))

    selling_price: int | None = None
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
            # "Value In Excess Of €700,000 EUR" / "Starting at $…" → low-only estimate.
            if low_only:
                estimate_low = amounts_main[0][0]
                currency = amounts_main[0][1]
            else:
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
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "estimate_currency": currency if has_estimate else None,
        "currency": currency or "USD",
    }


def _extract_spec_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Extract label→value pairs from RM Sotheby's lot spec panel.

    RM has changed their HTML structure across site redesigns. We try several
    selector strategies in priority order and merge the results so that any
    one matching strategy produces a full pair set.
    """
    pairs: dict[str, str] = {}

    # Strategy 1 (legacy): div.idlabel / div.iddata — try col-lg parent first,
    # then fall back to direct sibling / parent-child to handle layout variations.
    for lbl in soup.select("div.idlabel"):
        label = lbl.get_text(" ", strip=True)
        parent = lbl.find_parent("div", class_=lambda c: c and "col-lg" in str(c))
        data_el = parent.select_one("div.iddata") if parent else None
        if not data_el:
            data_el = lbl.find_next_sibling("div", class_="iddata")
        if not data_el and lbl.parent:
            data_el = lbl.parent.find("div", class_="iddata")
        if not data_el:
            continue
        val = re.sub(r"^\|\s*", "", data_el.get_text(" ", strip=True)).strip()
        if label and val and label not in pairs:
            pairs[label] = val

    # Strategy 2 (current redesign): lot-specs__label / lot-specs__value sibling divs.
    for lbl in soup.select("[class*='lot-specs__label'], [class*='lotSpecs__label'], [class*='spec-label']"):
        label = lbl.get_text(" ", strip=True)
        val_el = lbl.find_next_sibling() or lbl.parent.find_next_sibling()
        if val_el:
            val = val_el.get_text(" ", strip=True)
            if label and val and label not in pairs:
                pairs[label] = val

    # Strategy 3: generic <dt>/<dd> or <th>/<td> spec tables anywhere on the page.
    for dt in soup.select("dt"):
        label = dt.get_text(" ", strip=True)
        dd = dt.find_next_sibling("dd")
        if dd:
            val = dd.get_text(" ", strip=True)
            if label and val and label not in pairs:
                pairs[label] = val
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) == 2:
            label = cells[0].get_text(" ", strip=True)
            val = cells[1].get_text(" ", strip=True)
            if label and val and label not in pairs:
                pairs[label] = val

    # Strategy 4: any element whose class contains "spec" paired with next sibling.
    if not pairs:
        for el in soup.find_all(class_=re.compile(r"spec", re.I)):
            label = el.get_text(" ", strip=True)
            sibling = el.find_next_sibling()
            if sibling and label:
                val = sibling.get_text(" ", strip=True)
                if val and label not in pairs:
                    pairs[label] = val

    return pairs


_PAGE_MILEAGE_RE = re.compile(
    r"(?:odometer|mileage|showing|indicated|recorded|reads?|clocked)[\w\s,]*?(\d[\d,]{0,6})\s*"
    r"(?:original|actual|indicated|documented|approximately|approx\.?)?\s*(?:miles?|mi\.?|km|kilometres?|kilometers?)\b"
    r"|(?:^|[\s(])(\d[\d,]{0,6})\s*(?:original|actual|indicated|documented|approximately|approx\.?)?\s*"
    r"(?:miles?|mi\.?|km|kilometres?|kilometers?)\b",
    re.I,
)
_PAGE_KM_UNIT_RE = re.compile(r"\b(?:km|kilometres?|kilometers?)\b", re.I)


def _mileage_raw_from_page(soup: BeautifulSoup) -> str | None:
    """Scan page <li>/<p> elements for odometer/mileage prose sentences.

    Returns the raw matched substring (e.g. ``"2,478 kilometres"``) so that
    ``parse_mileage_mi`` can convert it; or ``None`` if nothing found.
    """
    for tag in soup.find_all(["li", "p"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        m = _PAGE_MILEAGE_RE.search(text)
        if m:
            # group(1) from odometer-keyword branch, group(2) from bare-number branch
            num_str = (m.group(1) or m.group(2) or "").replace(",", "")
            if not num_str:
                continue
            unit = "km" if _PAGE_KM_UNIT_RE.search(m.group(0)) else "miles"
            return f"{num_str} {unit}"
    return None


_CHASSIS_LABEL_RE = re.compile(
    r"chassis\s*(?:no\.?|number|#)?|vin|serial\s*(?:no\.?|number)?|frame\s*(?:no\.?|number)?",
    re.I,
)


def _vin_from_spec_pairs(pairs: dict[str, str]) -> str | None:
    """Return VIN/chassis value from spec pairs, trying all known label variants."""
    for key in (
        "VIN", "Chassis No.", "Chassis No", "Chassis Number", "Chassis",
        "Serial No.", "Serial No", "Serial Number", "Frame No.", "Frame No",
        "Frame Number",
    ):
        v = pairs.get(key)
        if v and v.strip():
            return v.strip()
    # Case-insensitive fallback: iterate all pairs and check label against regex.
    for label, val in pairs.items():
        if _CHASSIS_LABEL_RE.fullmatch(label.strip()):
            if val and val.strip():
                return val.strip()
    return None


def _vin_from_html(soup: BeautifulSoup) -> str | None:
    """Fallback: scan idlabel/iddata blocks directly when spec-pair extraction misses them."""
    for lbl_el in soup.find_all("div", class_="idlabel"):
        label = lbl_el.get_text(" ", strip=True)
        if not _CHASSIS_LABEL_RE.search(label):
            continue
        data_el = lbl_el.find_next_sibling("div", class_="iddata")
        if not data_el:
            parent = lbl_el.find_parent()
            data_el = parent.find("div", class_="iddata") if parent else None
        if data_el:
            val = data_el.get_text(" ", strip=True)
            if val and val.strip():
                return re.sub(r"^\|\s*", "", val).strip()
    return None


def parse_lot_detail_html(html: str) -> dict[str, Any]:
    """Parse spec panels + og:title from a lot detail page.

    Tries multiple CSS selector strategies to handle RM Sotheby's HTML across
    different site redesigns. Falls back to description-text extraction for any
    field still null after the structured parse.
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs = _extract_spec_pairs(soup)

    vin = _vin_from_spec_pairs(pairs) or _vin_from_html(soup)

    mileage = (
        pairs.get("Mileage") or pairs.get("Odometer") or pairs.get("Indicated Mileage")
        or pairs.get("Miles") or pairs.get("Kilometers")
    )
    ext = (
        pairs.get("Exterior Color") or pairs.get("Exterior Colour")
        or pairs.get("Exterior") or pairs.get("Colour") or pairs.get("Color")
        or pairs.get("Paint") or pairs.get("Body Color")
    )
    intc = (
        pairs.get("Interior Color") or pairs.get("Interior Colour")
        or pairs.get("Interior") or pairs.get("Upholstery") or pairs.get("Trim")
    )
    engine = pairs.get("Engine") or pairs.get("Engine Spec") or pairs.get("Engine No.")
    trans = pairs.get("Transmission") or pairs.get("Gearbox")
    location = pairs.get("Location")

    og = soup.find("meta", attrs={"property": "og:title"})
    og_title = og.get("content") if og and og.get("content") else None

    desc_el = (
        soup.select_one("section.lotdescription")
        or soup.select_one('section[class*="lotdescription"]')
        or soup.select_one('[class*="lot-description"]')
        or soup.select_one('[class*="lotDescription"]')
    )
    description = desc_el.get_text(" ", strip=True) if desc_el else None

    # Mileage: spec panel first, then scan full page <li>/<p> prose for odometer sentences.
    # RMS sometimes embeds mileage only in bullet points like
    # "Odometer shows just 2,478 kilometres at the time of cataloguing".
    if not mileage:
        mileage = _mileage_raw_from_page(soup)

    # Description-text fallback: if spec panel returned nothing useful, mine
    # the description prose using the shared extractor.
    if description and not all([ext, intc, engine, trans]):
        desc_fields = extract_description_fields(description)
        ext = ext or desc_fields.get("ext_color")
        intc = intc or desc_fields.get("int_color")
        engine = engine or desc_fields.get("engine")
        if not trans and desc_fields.get("transmission"):
            trans = desc_fields["transmission"]

    # Final fallback: scan full page text for engine/transmission when the spec
    # panel and description section both missed them (e.g. RM embeds specs inside
    # catalogue prose blocks that aren't captured by either selector strategy).
    if not engine or not trans:
        full_text = soup.get_text(" ", strip=True)
        all_desc_fields = extract_description_fields(full_text)
        engine = engine or all_desc_fields.get("engine")
        if not trans and all_desc_fields.get("transmission"):
            trans = all_desc_fields["transmission"]

    if trans:
        trans = normalize_transmission(trans) or trans

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


def _mileage_from_description(desc: str) -> int | None:
    """Extract mileage from free-text description when spec panel has no Mileage field."""
    if not desc:
        return None
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", desc)
    m = re.search(
        r"(?:^|[\s(])(\d{1,7})\s*(?:original|actual|indicated|documented|showing|approximately|approx\.?)?\s*(?:mi\.?|miles?)\b",
        text, re.I,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.search(r"(?:^|[\s(])(\d{1,7})\s*(?:km|kilometers?|kilometres?)\b", text, re.I)
    if m:
        try:
            return round(int(m.group(1)) * 0.621371)
        except ValueError:
            pass
    return None


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

    _mileage_raw_val = parse_mileage_mi(d.get("mileage_raw"))
    _mileage_val = _mileage_raw_val if _mileage_raw_val is not None else _mileage_from_description(d.get("description") or "")

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
        "mileage_mi": _mileage_val,
        "ext_color": d.get("ext_color"),
        "int_color": d.get("int_color"),
        "engine_spec": d.get("engine_spec"),
        "transmission": d.get("transmission"),
        "location": d.get("location"),
        "description": d.get("description"),
        "og_title": d.get("og_title"),
    }
