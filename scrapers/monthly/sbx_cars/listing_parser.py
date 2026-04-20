"""Parse SBX Cars /results cards + listing detail HTML."""

from __future__ import annotations

import re
from typing import Any

import pycountry
from bs4 import BeautifulSoup

from shared.normalizer import KM_TO_MILES, country_code_to_name, normalize_transmission

from .config import BASE_URL

_LISTING_HREF_RE = re.compile(r"/listing/(\d+)/([^/?#]+)/?", re.I)


def _card_row_score(row: dict[str, Any]) -> tuple[int, int]:
    """
    When the DOM repeats the same listing, prefer the row that still has the
    public ``Sold for …`` line (primary card) and richer location text.
    """
    has_price = 1 if row.get("card_amount") is not None else 0
    loc_len = len((row.get("location") or "").strip())
    return (has_price, loc_len)


def _pick_better_card(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    if _card_row_score(new) > _card_row_score(existing):
        return new
    if _card_row_score(existing) > _card_row_score(new):
        return existing
    return existing

# Card text: "Sold for US$227,000" / "Sold for 1,130,000 AED" / "Sold for €1,050,000"
# AED must be tried before USD because AED cards read "Sold for 1,130,000 AED"
# and the USD pattern (if US$ is absent) would otherwise greedily match the number.
_SOLD_FOR_AED = re.compile(
    r"Sold\s+for\s*([\d,]+)\s*AED",
    re.I,
)
_SOLD_FOR_USD = re.compile(
    r"Sold\s+for\s*US\$\s*([\d,]+)",
    re.I,
)
_SOLD_FOR_EUR = re.compile(
    r"Sold\s+for\s*€\s*([\d,]+)|Sold\s+for\s*([\d,]+)\s*EUR",
    re.I,
)


def _int_from_commas(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace(",", "").strip()
    if not s.isdigit():
        return None
    return int(s)


def parse_sold_for_from_card_text(text: str) -> tuple[int | None, str | None]:
    """Return ``(amount, currency)`` from flattened card text, if a public price is shown."""
    if not text:
        return None, None
    t = " ".join(text.split())
    m = _SOLD_FOR_AED.search(t)
    if m:
        v = _int_from_commas(m.group(1))
        return (v, "AED") if v is not None else (None, None)
    m = _SOLD_FOR_USD.search(t)
    if m:
        v = _int_from_commas(m.group(1))
        return (v, "USD") if v is not None else (None, None)
    m = _SOLD_FOR_EUR.search(t)
    if m:
        g1, g2 = m.group(1), m.group(2)
        raw = g1 or g2
        v = _int_from_commas(raw)
        return (v, "EUR") if v is not None else (None, None)
    return None, None


_ENGINE_RE = re.compile(
    r"\b(\d[\d.]*[\s-]*[Ll](?:iter|itre)?|[Vv]\d{1,2}(?:[-\s]\d+)?|[Ff]lat[-\s]?\d|[Hh]orizontally[-\s]opposed|turbo|supercharg|electric|hybrid|rotary|[Ww]\d{1,2})\b",
    re.I,
)
_TRANS_RE = re.compile(
    r"\b(\d[-\s]?speed|manual|automatic|DCT|PDK|DSG|SMG|CVT|semi[-\s]auto|dual[-\s]clutch|gearbox|transaxle)\b",
    re.I,
)


def _section_text(soup: BeautifulSoup, heading: str) -> str:
    """Return plain text from the ``paragraph-box`` under a named ``<h2>``."""
    h2 = soup.find("h2", string=re.compile(rf"^\s*{re.escape(heading)}\s*$", re.I))
    if not h2:
        return ""
    box = h2.find_next_sibling("div")
    if not box:
        return ""
    return box.get_text(" ", strip=True)


def _highlights_bullets(soup: BeautifulSoup) -> list[str]:
    h2 = soup.find("h2", string=re.compile(r"^\s*Highlights\s*$", re.I))
    if not h2:
        return []
    box = h2.find_next_sibling("div")
    if not box:
        return []
    return [li.get_text(strip=True) for li in box.find_all("li") if li.get_text(strip=True)]


def _engine_from_highlights(bullets: list[str]) -> str | None:
    for b in bullets:
        if _ENGINE_RE.search(b):
            return b
    return None


def _transmission_from_highlights(bullets: list[str]) -> str | None:
    for b in bullets:
        if _TRANS_RE.search(b) and not _ENGINE_RE.search(b):
            return normalize_transmission(b) or b
    for b in bullets:
        if _TRANS_RE.search(b):
            return normalize_transmission(b) or b
    return None


def _slice_after(html: str, needle: str, length: int = 1500) -> str:
    pos = html.find(needle)
    if pos < 0:
        return ""
    return html[pos : pos + length]


def _country_display_from_code(code: str | None) -> str | None:
    if not code:
        return None
    u = code.strip().upper()
    if u == "ARE":
        return "UAE"
    if len(u) == 3:
        c = pycountry.countries.get(alpha_3=u)
        if c:
            return country_code_to_name(c.alpha_2)
    if len(u) == 2:
        return country_code_to_name(u)
    return u


# Sections that collectively make the full listing description
_DESC_SECTIONS = [
    "Overview",
    "Specifications & Options",
    "Service History",
    "Condition & Reports",
    "Included in the Sale",
    "Summary",
]


def _build_full_description(soup: BeautifulSoup, highlights: list[str]) -> str | None:
    parts: list[str] = []
    if highlights:
        parts.append("Highlights: " + " | ".join(highlights))
    for section in _DESC_SECTIONS:
        text = _section_text(soup, section)
        if text:
            parts.append(f"{section}: {text}")
    return "\n\n".join(parts)[:200_000] if parts else None


def parse_detail_html(html: str | None) -> dict[str, Any]:
    """
    Pull auction + vehicle fields from a listing page.

    JSON-level fields (VIN, year/make/model, auction status, price, dates, colors,
    mileage) come from the embedded escaped JSON.  Description, engine, and
    transmission come from the rendered HTML sections which are richer and already
    decoded.
    """
    if not html:
        return {}

    out: dict[str, Any] = {}
    soup = BeautifulSoup(html, "lxml")

    # ── VIN ────────────────────────────────────────────────────────────────────
    m = re.search(r'"IdentificationNumber"\s*:\s*"([A-HJ-NPR-Z0-9]{17})"', html)
    if m:
        out["vin"] = m.group(1)
    else:
        m = re.search(r'IdentificationNumber\\":\\"([A-HJ-NPR-Z0-9]{17})', html)
        if m:
            out["vin"] = m.group(1)

    # ── Year / Make / Model ────────────────────────────────────────────────────
    m = re.search(
        r'\\"Year\\":(\d{4}),\\"BrandName\\":\\"([^"\\]*)\\",\\"ModelName\\":\\"([^"\\]*)\\"',
        html,
    )
    if m:
        out["year"] = int(m.group(1))
        out["make"] = m.group(2).strip() or None
        out["model"] = m.group(3).strip() or None

    sub_m = re.search(r'\\"Subtitle\\":\\"([^"\\]*)\\"', html)
    if sub_m and sub_m.group(1).strip():
        out["subtitle"] = sub_m.group(1).strip()

    # ── Auction block (status, price, currency, end date) ─────────────────────
    auc = _slice_after(html, '\\"Auction\\":{', 1500)
    if auc:
        sm = re.search(r'\\"Status\\":\\"([^"\\]+)\\"', auc)
        if sm:
            out["auction_status"] = sm.group(1)
        spm = re.search(r'\\"SalePrice\\":(\d+)', auc)
        if spm:
            out["sale_price"] = int(spm.group(1))
        cm = re.search(r'\\"CurrencyCode\\":\\"([A-Z]{3})\\"', auc)
        if cm:
            out["currency_code"] = cm.group(1)
        etm = re.search(r'\\"ActualEndTime\\":\\"(\d{4}-\d{2}-\d{2})', auc)
        if etm:
            out["selling_date"] = etm.group(1)

    # ── Cache block (highest bid, bid count, internal listing id) ─────────────
    cache = _slice_after(html, '\\"Cache\\":{', 700)
    if cache:
        hm = re.search(r'\\"HighestBid\\":(\d+)', cache)
        if hm:
            out["highest_bid"] = int(hm.group(1))
        bcm = re.search(r'\\"BidCount\\":(\d+)', cache)
        if bcm:
            out["bid_count"] = int(bcm.group(1))
        lid_m = re.search(r'\\"ListingId\\":(\d+)', cache)
        if lid_m:
            out["listing_id_num"] = int(lid_m.group(1))

    # ── Colors + mileage + transmission (from spec JSON block) ───────────────
    # Extend slice to 1400 chars so TransaxleModel (after Engine/EngineNumber/
    # TransmissionType) is reliably included. Searching the full HTML would pick
    # up sidebar "Ending Soon" listings that also embed JSON.
    spec = _slice_after(html, '\\"Exterior\\":\\"', 1400)
    if spec:
        em = re.search(r'\\"Exterior\\":\\"([^"\\]*)\\"', spec)
        if em and em.group(1).strip():
            out["ext_color"] = em.group(1).strip()
        im = re.search(r'\\"Interior\\":\\"([^"\\]*)\\"', spec)
        if im and im.group(1).strip():
            out["int_color"] = im.group(1).strip()
        um = re.search(
            r'\\"Usage\\":\{[^}]*?\\"Value\\":(\d+)[^}]*?\\"Unit\\":\\"([^"\\]+)\\"',
            spec,
        )
        if um:
            val = int(um.group(1))
            unit = um.group(2).lower()
            if "mile" in unit:
                out["mileage_mi"] = val
            elif "km" in unit or "kilometer" in unit:
                out["mileage_mi"] = int(round(val * KM_TO_MILES))
        # TransaxleModel holds the actual gearbox type; TransmissionType is always "Transmission"
        tx_m = re.search(r'\\"TransaxleModel\\":\\"([^"\\]+)\\"', spec)
        if tx_m and tx_m.group(1).strip():
            out["transmission"] = normalize_transmission(tx_m.group(1).strip())

    # ── Engine + Transmission from Highlights bullets (richer than JSON) ───────
    bullets = _highlights_bullets(soup)
    engine_hl = _engine_from_highlights(bullets)
    if engine_hl:
        out["engine_spec"] = engine_hl
    if not out.get("transmission"):
        tx_hl = _transmission_from_highlights(bullets)
        if tx_hl:
            out["transmission"] = tx_hl

    # ── Engine fallback: scan Overview text for first engine mention ───────────
    if not out.get("engine_spec"):
        overview = _section_text(soup, "Overview")
        if overview:
            for sent in re.split(r"[.!?]", overview):
                if _ENGINE_RE.search(sent):
                    snippet = sent.strip()
                    if 5 < len(snippet) < 200:
                        out["engine_spec"] = snippet
                    break

    # ── Full description from rendered page sections ───────────────────────────
    desc = _build_full_description(soup, bullets)
    if desc:
        out["listing_description"] = desc

    # ── Country / location from embedded JSON address block ────────────────────
    addr_m = re.search(
        r'CountryCode\\":\\"([A-Z]{2,3})\\",\\"StateName\\":\\"([^"\\]*)\\",\\"RegionName\\":\\"[^"\\]*\\",\\"CityName\\":\\"([^"\\]*)\\"',
        html,
    )
    if addr_m:
        code, state, city = addr_m.group(1), addr_m.group(2).strip(), addr_m.group(3).strip()
        disp = _country_display_from_code(code)
        loc_bits = [p for p in (city, state, disp) if p]
        if loc_bits:
            out["detail_location"] = ", ".join(loc_bits)
        if disp:
            out["detail_country"] = disp

    return out


def resolve_price_from_detail(d: dict[str, Any]) -> tuple[int | None, str | None]:
    """
    Derive hammer / published price from parsed detail when the card shows no amount.

    ``SalePrice`` is often ``0`` even when sold; public lots use ``HighestBid`` for hammer.
    Private sales do not publish a final price — returns ``(None, None)``.
    """
    if not d:
        return None, None
    status = (d.get("auction_status") or "").strip()
    cur = (d.get("currency_code") or "USD").upper()
    sp = d.get("sale_price")
    if isinstance(sp, int) and sp > 0:
        return sp, cur
    hb = d.get("highest_bid")
    if status == "Sold" and isinstance(hb, int) and hb > 0:
        return hb, cur
    return None, None


def parse_results_cards(html: str) -> list[dict[str, Any]]:
    """Parse ``/results`` HTML into one dict per listing (deduped by numeric listing id)."""
    soup = BeautifulSoup(html or "", "lxml")
    by_id: dict[str, dict[str, Any]] = {}

    for wrap in soup.select("div.vehicle-item-wrapper"):
        a = wrap.select_one('a.vehicle-gallery-wrapper[href*="/listing/"]')
        if not a:
            continue
        href = (a.get("href") or "").strip()
        m = _LISTING_HREF_RE.match(href)
        if not m:
            continue
        lid, slug = m.group(1), m.group(2)
        year_el = wrap.select_one(".vehicle-title span")
        year_txt = year_el.get_text(strip=True) if year_el else ""
        year: int | None = int(year_txt) if year_txt.isdigit() else None

        h3 = wrap.select_one(".vehicle-title h3.heading-2")
        make = h3.get_text(strip=True) if h3 else ""

        spans = wrap.select(".vehicle-model-name span")
        model = spans[0].get_text(strip=True) if spans else ""
        sub = spans[1].get_text(strip=True) if len(spans) > 1 else ""
        model_full = " ".join(p for p in (model, sub) if p).strip()

        title = " ".join(p for p in (year_txt, make, model_full) if p).strip()

        img = wrap.select_one("a.vehicle-gallery-wrapper img[src]")
        image_url: str | None = None
        if img:
            src = (img.get("src") or "").strip()
            if src.startswith("//"):
                image_url = "https:" + src
            elif src.startswith("/"):
                image_url = f"{BASE_URL}{src}"
            elif src.startswith("http"):
                image_url = src

        city_el = wrap.select_one(".location-text .city")
        st_el = wrap.select_one(".location-text .state")
        ctry_el = wrap.select_one(".location-text .country")
        loc_parts = []
        if city_el:
            loc_parts.append(city_el.get_text(" ", strip=True).rstrip(","))
        if st_el:
            loc_parts.append(st_el.get_text(" ", strip=True).rstrip(","))
        if ctry_el:
            loc_parts.append(ctry_el.get_text(" ", strip=True))
        location = ", ".join(loc_parts) if loc_parts else None
        country = ctry_el.get_text(strip=True) if ctry_el else None

        card_text = wrap.get_text(" ", strip=True)
        camt, ccur = parse_sold_for_from_card_text(card_text)

        row = {
            "listing_id": lid,
            "slug": slug,
            "listing_path": f"/listing/{lid}/{slug}",
            "listing_url": f"{BASE_URL}/listing/{lid}/{slug}",
            "title": title or None,
            "year": year,
            "make": make or None,
            "model": model_full or None,
            "image_url": image_url,
            "location": location,
            "country": country,
            "card_amount": camt,
            "card_currency": ccur,
        }
        if lid in by_id:
            by_id[lid] = _pick_better_card(by_id[lid], row)
        else:
            by_id[lid] = row

    return list(by_id.values())


def merge_card_and_detail(card: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Combine results-card row + parsed listing HTML."""
    year = card.get("year")
    make = card.get("make")
    model = card.get("model")
    if detail.get("year") is not None:
        year = detail["year"]
    if detail.get("make"):
        make = detail["make"]
    if detail.get("model"):
        model = detail["model"]

    title = card.get("title") or ""
    if year and make:
        mpart = model or ""
        title = " ".join(str(x) for x in (year, make, mpart) if x).strip()

    amt, cur = None, None
    if card.get("card_amount") is not None and card.get("card_currency"):
        amt, cur = card["card_amount"], card["card_currency"]
    else:
        amt, cur = resolve_price_from_detail(detail)

    status = (detail.get("auction_status") or "").strip()
    if status in ("Sold", "SoldPrivately"):
        sold_status = "Sold"
    elif not status and card.get("card_amount") is not None:
        sold_status = "Sold"
    else:
        sold_status = "Unsold"

    if sold_status == "Unsold":
        amt, cur = None, None

    out: dict[str, Any] = {
        "record_id": card["listing_id"],
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "sold_status": sold_status,
        "amount": amt,
        "currency": (cur or "USD").upper() if amt is not None else "USD",
        "image_url": card.get("image_url"),
        "listing_url": card.get("listing_url"),
        "location": detail.get("detail_location") or card.get("location"),
        "country": detail.get("detail_country") or card.get("country"),
        "vin": detail.get("vin"),
        "mileage_mi": detail.get("mileage_mi"),
        "ext_color": detail.get("ext_color"),
        "int_color": detail.get("int_color"),
        "engine_spec": detail.get("engine_spec"),
        "transmission": detail.get("transmission"),
        "lot_number": detail.get("listing_id_num"),
        "listing_description": detail.get("listing_description"),
        "bid_count": detail.get("bid_count"),
        "selling_date": detail.get("selling_date"),
    }
    return out
