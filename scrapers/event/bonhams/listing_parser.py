"""Bonhams Cars — Typesense auction hits + Next.js lot pages."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import extract_description_fields

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

# ``Chassis no. WP0ZZZ96ZPS470173`` — Bonhams labels it ``Chassis no.``.
#
# Capture is whitespace-FREE: the source HTML has the chassis and engine
# numbers in adjacent ``<dd>`` blocks that get joined with a space during
# ``html_to_text``, producing prose like
# ``Chassis no. WP0ZZZ96ZPS470173 Engine no. M96…``.
# The previous capture group included ``\s`` and a 30-char max, which
# greedily grabbed through the space and into the ``Engine no.`` label —
# the subsequent ``re.sub(r"\s+", "")`` then collapsed the space out,
# yielding VINs like ``WP0ZZZ96ZPS470173ENGINENO`` (see P1 §1.2 bug report).
# ``[A-Z0-9\-*/]`` with no space stops at the first whitespace boundary.
_CHASSIS_RE = re.compile(
    r"(?:Chassis\s*(?:no\.?|Number)|VIN)\s*[:.#]?\s*\*?([A-Z0-9][A-Z0-9\-./]{2,24})",
    re.I,
)
_VIN_LIKE_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.I)

# Bullet lines in ``sCatalogDesc`` often contain mileage hints:
#   ``•	Circa 163,200 kilometres recorded``
#   ``•	Only 12,345 miles from new``
_MILEAGE_KM_RE = re.compile(r"([\d,]+)\s*(?:kilometres|kilometers|km)\b", re.I)
_MILEAGE_MI_RE = re.compile(r"([\d,]+)\s*(?:miles|mile|mi)\b", re.I)

# Transmission mentions — Bonhams prose is inconsistent, so look for either a
# compact bullet (``• Manual transmission``) or a hint in the running text.
_TRANS_MANUAL_RE = re.compile(
    r"\b(?:manual|stick[-\s]?shift|\d+[-\s]?speed\s+manual|H[-\s]?pattern)\b", re.I,
)
_TRANS_AUTO_RE = re.compile(
    r"\b(?:automatic|PDK|DCT|Tiptronic|Sportmatic|Powerglide|Hydra[-\s]?Matic|DSG)\b", re.I,
)

# Engine mentions — capture a displacement + cylinder form (e.g. ``5.0-litre flat-12``,
# ``3.6-Liter V8``). Keep it short; the full description is saved separately.
_ENGINE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)[-\s]?(?:liter|litre|l)\b[^.\n]{0,60}?"
    r"\b(?:V\d{1,2}|flat[-\s]?\d{1,2}|straight[-\s]?\d|inline[-\s]?\d|boxer[-\s]?\d)\b",
    re.I,
)

# Paint / exterior colour. Bonhams prose uses phrases like
# ``Finished in Rosso Corsa over a Nero interior``. Require a colour-like
# head-word (capitalised; not a common English connective) and cap length.
_EXT_COLOR_RE = re.compile(
    r"(?:finished|painted|repainted)\s+in\s+"
    r"([A-Z][A-Za-z][A-Za-z\s'’-]{2,30}?)"
    r"(?=\s+(?:paint|bodywork|over|with|and|,|\.|$))",
    re.I,
)

# Interior colour — ``<colour> leather/upholstery/hide``. Limit the head-word
# to short colour-like phrases (≤3 words) to avoid false positives like
# ``Finished in factory Black with Tan``. Case-insensitive so "TAN leather"
# and "tan leather" both match.
_INT_COLOR_RE = re.compile(
    r"\b([A-Za-z]+(?:\s+[A-Za-z]+){0,2})\s+"
    r"(?:leather|upholstery|hide|cloth|vinyl)\b",
    re.IGNORECASE,
)


def _extract_first_match(rx: re.Pattern[str], text: str) -> str | None:
    if not text:
        return None
    m = rx.search(text)
    if not m:
        return None
    s = (m.group(1) if m.groups() else m.group(0)).strip()
    # Guard against over-greedy colour phrases like "Matching chassis".
    banned = {"matching", "original", "its", "a", "the", "new", "racing", "competition"}
    if s.split()[0].lower() in banned and len(s.split()) == 1:
        return None
    return s or None


def extract_transmission(desc_text: str) -> str | None:
    """``Manual`` / ``Automatic`` from free-text description."""
    if not desc_text:
        return None
    if _TRANS_AUTO_RE.search(desc_text):
        return "Automatic"
    if _TRANS_MANUAL_RE.search(desc_text):
        return "Manual"
    return None


def extract_engine_spec(desc_text: str) -> str | None:
    """``5.0-litre flat-12`` style — tight capture, no prose."""
    if not desc_text:
        return None
    m = _ENGINE_RE.search(desc_text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip().rstrip(",.")


def extract_ext_color(desc_text: str) -> str | None:
    return _extract_first_match(_EXT_COLOR_RE, desc_text or "")


def extract_int_color(desc_text: str) -> str | None:
    if not desc_text:
        return None
    m = _INT_COLOR_RE.search(desc_text)
    if not m:
        return None
    s = m.group(1).strip()
    # Reject common non-colour leaders.
    banned = {"finished", "matching", "original", "its", "the", "new", "full"}
    if s.split()[0].lower() in banned:
        return None
    return s


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
        _raw_country = (doc.get("country") or {}).get("name")
        _country_code = (doc.get("country") or {}).get("code")
        # Monaco has ISO code MC. Some Typesense datasets incorrectly map it to
        # France because of gazetteer ambiguity. Force the correct sovereign state.
        if _country_code and _country_code.upper() == "MC":
            _raw_country = "Monaco"
        # Also catch the "Monte Carlo" city string making it through as a country name.
        if _raw_country and _raw_country.lower() in ("monte carlo", "monte-carlo"):
            _raw_country = "Monaco"

        au = {
            "id": str(doc.get("id") or ""),
            "slug": str(doc.get("slug") or ""),
            "title": doc.get("auctionTitle") or doc.get("auctionHeading") or "",
            "type": doc.get("auctionType"),
            "country": _raw_country,
            "country_code": _country_code,
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
        # Vintage/short chassis numbers (e.g. "1175", "418") — accept from 3 chars.
        if 3 <= len(candidate) <= 25:
            return candidate
    return None


# Bonhams URL slugs embed the chassis number as: …chassis-no-<value>(-engine-no-…|end)
# Example: /auction/31711/lot/115/1985-ferrari-testarossa-coupe-chassis-no-zffta17b000055031-engine-no-f113a-00060/
_URL_CHASSIS_RE = re.compile(
    r"(?:chassis-no|vin)-([^/]+?)(?=-engine-no-|-frame-no-|-reg-no-|/?$)",
    re.IGNORECASE,
)


def extract_vin_from_url(listing_url: str) -> str | None:
    """Extract chassis number from a Bonhams lot URL slug when description extraction fails.

    Bonhams consistently encodes ``chassis-no-<value>`` in the URL path for 100%
    of current listings. This function acts as a fallback after
    ``extract_vin_from_title_or_desc`` so that lots where the chassis appears
    only in the URL (and not in the description body) still get a VIN populated.
    Description-extracted VINs take priority because Bonhams sometimes truncates
    the URL for SEO while keeping the full chassis string in the description.

    The regex allows internal hyphens in chassis numbers (e.g. ``abc-123``) while
    stopping before the ``-engine-no-`` delimiter so the two values don't merge.
    """
    if not listing_url:
        return None
    m = _URL_CHASSIS_RE.search(listing_url)
    if not m:
        return None
    candidate = m.group(1).upper()
    if 3 <= len(candidate) <= 25:
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


def _parse_structured_mileage(lot: dict[str, Any]) -> int | None:
    """Extract mileage from Bonhams __NEXT_DATA__ structured fields.

    Bonhams exposes several possible mileage keys in the lot JSON:
      dMileage / dKilometres / dOdometer — numeric values
      sMileageUnit / sOdometerUnit       — unit string ("Miles", "Kilometres", etc.)
    We try each in priority order and convert to integer miles.
    """
    # Try miles-first fields.
    for key in ("dMileage",):
        raw = lot.get(key)
        if raw is not None:
            try:
                val = float(str(raw).replace(",", ""))
                if val > 0:
                    unit = (lot.get("sMileageUnit") or "").lower()
                    if "km" in unit or "kilo" in unit:
                        return int(round(val * 0.621371))
                    return int(round(val))
            except (ValueError, TypeError):
                pass

    # Try km-first fields.
    for key in ("dKilometres", "dOdometer"):
        raw = lot.get(key)
        if raw is not None:
            try:
                val = float(str(raw).replace(",", ""))
                if val > 0:
                    unit = (lot.get("sOdometerUnit") or lot.get("sMileageUnit") or "").lower()
                    if "mile" in unit or "mi" == unit.strip():
                        return int(round(val))
                    # Default assumption for these keys is km.
                    return int(round(val * 0.621371))
            except (ValueError, TypeError):
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

    # Structured mileage fields take priority over description-text regex.
    mileage_mi = _parse_structured_mileage(lot) or parse_mileage_mi_from_desc(desc_text)

    # Use the shared description extractor which has richer, battle-tested patterns
    # for engine, transmission, and both color fields.
    desc_fields = extract_description_fields(desc_text) if desc_text else {}
    engine_spec = desc_fields.get("engine")
    transmission = desc_fields.get("transmission")
    ext_color = desc_fields.get("ext_color")
    int_color = desc_fields.get("int_color")

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
        "transmission": transmission,
        "engine_spec": engine_spec,
        "ext_color": ext_color,
        "int_color": int_color,
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
