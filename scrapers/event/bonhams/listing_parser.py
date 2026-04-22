"""Bonhams Cars — Typesense auction hits + Next.js lot pages."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from .config import SITE_ORIGIN

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)

# "1993 Porsche 911 Type 964 Turbo 3.6 Coupé Chassis no. WP0…" — split off the
# ``Chassis no.`` / ``Engine no.`` tail that Bonhams always appends.
_CHASSIS_TAIL_RE = re.compile(
    r"\s*(?:Chassis\s*no\.?|Engine\s*no\.?|Frame\s*no\.?|Registration\s*no\.?|VIN)\b.*$",
    re.I,
)
_YEAR_LEADING_RE = re.compile(r"^(?P<year>(?:19|20)\d{2})\b\s+(?P<rest>.+)$")

# ``Chassis no. WP0ZZZ96ZPS470173`` — Bonhams labels it ``Chassis no.``; VIN-like tokens only.
_CHASSIS_RE = re.compile(
    r"Chassis\s*(?:no\.?|Number)\s*[:.]?\s*([A-Z0-9][A-Z0-9\-*/ ]{4,30})",
    re.I,
)
_VIN_LIKE_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.I)

# Bullet lines in ``sCatalogDesc`` often contain mileage hints:
#   ``•	Circa 163,200 kilometres recorded``
#   ``•	Only 12,345 miles from new``
_MILEAGE_KM_RE = re.compile(r"([\d,]+)\s*(?:kilometres|kilometers|km)\b", re.I)
_MILEAGE_MI_RE = re.compile(r"([\d,]+)\s*(?:miles|mile|mi)\b", re.I)


def extract_next_data(html: str) -> dict[str, Any] | None:
    """Parse ``__NEXT_DATA__`` JSON from a Next.js-rendered Bonhams page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning("Failed to decode __NEXT_DATA__: %s", e)
        return None


def parse_auction_hits(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Return ``(auctions, total_found)`` from a Typesense ``multi_search`` response."""
    if not isinstance(payload, dict):
        return [], 0
    results = payload.get("results") or []
    if not results:
        return [], 0
    first = results[0] or {}
    found = int(first.get("found") or 0)
    out: list[dict[str, Any]] = []
    for hit in first.get("hits") or []:
        doc = hit.get("document") or {}
        if not doc:
            continue
        au = {
            "id": str(doc.get("id") or ""),
            "slug": str(doc.get("slug") or ""),
            "title": doc.get("auctionTitle") or doc.get("auctionHeading") or "",
            "type": doc.get("auctionType"),
            "country": (doc.get("country") or {}).get("name"),
            "country_code": (doc.get("country") or {}).get("code"),
            "currency_iso": (doc.get("currency") or {}).get("iso_code"),
            "hammer_datetime": (doc.get("hammerTime") or {}).get("datetime"),
            "hammer_timestamp": (doc.get("hammerTime") or {}).get("timestamp"),
            "total_lots": (doc.get("lots") or {}).get("total"),
            "departments": [d.get("code") for d in (doc.get("departments") or [])],
        }
        if au["id"] and au["slug"]:
            out.append(au)
    return out, found


def parse_lots_from_auction_page(html: str) -> tuple[list[dict[str, Any]], int]:
    """Return ``(lots, nbHits)`` from an auction results HTML page."""
    data = extract_next_data(html)
    if not data:
        return [], 0
    lot_data = (
        data.get("props", {}).get("pageProps", {}).get("lotData", {}) or {}
    )
    nb = int(lot_data.get("nbHits") or 0)
    return list(lot_data.get("auctionLots") or []), nb


def build_lot_detail_url(auction_id: str, lot_no: str, slug: str) -> str:
    return f"{SITE_ORIGIN}/auction/{auction_id}/lot/{lot_no}/{slug}/"


def is_car_lot(lot: dict[str, Any]) -> bool:
    """True for cars department (``MOT-CAR``); excludes motorcycles/automobilia."""
    dep = lot.get("department") or {}
    return (dep.get("code") or "").upper() == "MOT-CAR"


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return html_lib.unescape(re.sub(r"\s+", " ", s)).strip()


def parse_title_for_year_make_model(
    sdesc: str,
) -> tuple[int | None, str | None, str | None, str]:
    """``1993 Porsche 911 Type 964 Turbo 3.6 Coupé Chassis no. WP0…`` → parts + cleaned title."""
    t = _clean_text(sdesc)
    t = _CHASSIS_TAIL_RE.sub("", t).strip()
    m = _YEAR_LEADING_RE.match(t)
    if not m:
        return None, None, t or None, t
    year = int(m.group("year"))
    rest = m.group("rest").strip()
    parts = rest.split(None, 1)
    if len(parts) == 2:
        return year, parts[0], parts[1], t
    if len(parts) == 1:
        return year, parts[0], None, t
    return year, None, None, t


def extract_vin_from_title_or_desc(sdesc: str, desc_text: str) -> str | None:
    for src in (sdesc or "", desc_text or ""):
        m = _CHASSIS_RE.search(src)
        if not m:
            continue
        candidate = re.sub(r"\s+", "", m.group(1)).upper()
        if _VIN_LIKE_RE.match(candidate):
            return candidate
        # Some older cars have non-17-char chassis numbers — still return them.
        if 5 <= len(candidate) <= 25:
            return candidate
    return None


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n", strip=True)
    text = html_lib.unescape(text)
    # Collapse 3+ newlines to 2 for readability.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_mileage_mi_from_desc(desc_text: str) -> int | None:
    if not desc_text:
        return None
    m_km = _MILEAGE_KM_RE.search(desc_text)
    m_mi = _MILEAGE_MI_RE.search(desc_text)
    if m_mi:
        try:
            return int(m_mi.group(1).replace(",", ""))
        except ValueError:
            pass
    if m_km:
        try:
            km = int(m_km.group(1).replace(",", ""))
            return int(round(km * 0.621371))
        except ValueError:
            pass
    return None


def parse_lot_detail_next_data(html: str) -> dict[str, Any] | None:
    """Pull the detail ``lot`` dict + cleaned description from a lot detail HTML page."""
    data = extract_next_data(html)
    if not data:
        return None
    lot = data.get("props", {}).get("pageProps", {}).get("lot") or {}
    if not lot:
        return None

    desc_html = lot.get("sCatalogDesc") or ""
    desc_text = html_to_text(desc_html)
    sdesc = lot.get("sDesc") or ""

    year, make, model, clean_title = parse_title_for_year_make_model(sdesc)
    vin = extract_vin_from_title_or_desc(sdesc, desc_text)
    mileage_mi = parse_mileage_mi_from_desc(desc_text)

    images = lot.get("images") or []
    image_url = None
    if images:
        first = images[0] or {}
        image_url = first.get("image_url") or None

    status = (lot.get("sLotStatus") or "").upper() or None
    hammer_price = lot.get("dHammerPrice")
    hammer_premium = lot.get("dHammerPremium")
    est_low = lot.get("dEstimateLow")
    est_high = lot.get("dEstimateHigh")
    currency = (lot.get("currency") or {}).get("iso_code")
    hammer_time = (lot.get("hammerTime") or {}).get("datetime")
    sale_no = lot.get("iSaleNo")
    if isinstance(sale_no, dict):
        sale_no = sale_no.get("iSaleNo")

    return {
        "auction_id": str(sale_no or "") or None,
        "lot_no": str(lot.get("iSaleLotNo") or ""),
        "title": clean_title or sdesc,
        "year": year,
        "make": make,
        "model": model,
        "vin": vin,
        "mileage_mi": mileage_mi,
        "description": desc_text or None,
        "status": status,
        "hammer_price": hammer_price,
        "hammer_premium": hammer_premium,
        "estimate_low": est_low,
        "estimate_high": est_high,
        "currency": currency,
        "hammer_time": hammer_time,
        "image_url": image_url,
    }
