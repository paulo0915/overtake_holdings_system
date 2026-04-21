"""Parse Broad Arrow HTML pages — past-auctions index, event pages, results listing, vehicle detail."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from shared.normalizer import normalize_engine_spec

from scrapers.event.mecum import extract_vin_from_description

from .config import RESULTS_PATH, SITE_ORIGIN, SORT_PARAM, SORT_VALUE

logger = logging.getLogger(__name__)

# --- Regexes -----------------------------------------------------------------

_EVENT_PATH_RE = re.compile(r'href=["\'](/events/event/[^"\']+)["\']', re.IGNORECASE)

_BRANCH_RESULTS_URL_RE = re.compile(
    r'href=["\']('
    r'https?://www\.broadarrowauctions\.com/vehicles/results\?[^"\']*branch_id_eq[^"\']*'
    r')["\']',
    re.IGNORECASE,
)

_VEHICLE_PATH_RE = re.compile(
    r'href=["\'](?:https?://www\.broadarrowauctions\.com)?'
    r'(/vehicles/([a-z0-9_]+/[0-9]{4}-[^"\'&\s]+))["\']',
    re.IGNORECASE,
)

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_CONVERT_PRICE_RE = re.compile(
    r'<span[^>]*id=[\'"]convert_price[\'"][^>]*>\s*(\$[\d,]+)',
    re.IGNORECASE,
)
_LOT_BRANCH_RE = re.compile(
    r'<p[^>]+class=[\'"]lot-and-branch[\'"][^>]*>(.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)

# --- Discovery parsers -------------------------------------------------------


def parse_event_paths_from_past_auctions(html: str | None) -> list[str]:
    """Return unique ``/events/event/{name}`` paths from the /past-auctions page."""
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _EVENT_PATH_RE.finditer(html):
        p = html_lib.unescape(m.group(1)).strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def parse_branch_results_url_from_event(html: str | None) -> str | None:
    """Extract the branch-specific results URL from an event page.

    The event page contains a link like:
        /vehicles/results?q[branch_id_eq]=30&q[s][0][name_dir]=stock.asc

    We replace the sort with ``sales_price.desc`` so results are ordered highest
    price first (consistent across all events), and return the full URL.
    """
    if not html:
        return None
    m = _BRANCH_RESULTS_URL_RE.search(html)
    if not m:
        return None
    raw_url = html_lib.unescape(m.group(1))
    # Extract branch_id_eq value
    bid = re.search(r'branch_id_eq.*?=(\d+)', raw_url)
    if not bid:
        return None
    branch_id = bid.group(1)
    return (
        f"{SITE_ORIGIN}{RESULTS_PATH}"
        f"?q[branch_id_eq]={branch_id}&{SORT_PARAM}={SORT_VALUE}"
    )


def parse_vehicle_paths_from_results(html: str | None) -> list[str]:
    """Extract unique ``auction_lot/year-make-model`` paths from a results page."""
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _VEHICLE_PATH_RE.finditer(html):
        p = m.group(2).strip().rstrip("/")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def next_results_page_url(html: str | None) -> str | None:
    """Return the absolute URL from ``rel=next`` if present."""
    if not html:
        return None
    m = re.search(r'rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']', html, re.I)
    if not m:
        return None
    href = html_lib.unescape(m.group(1).strip())
    if href.startswith("/"):
        return f"{SITE_ORIGIN}{href}"
    if href.startswith("http"):
        return href
    return urljoin(SITE_ORIGIN, href)


# --- Detail page parser ------------------------------------------------------


def _parse_money_us(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\$([\d,]+)", s)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def _schema_price_usd(offers: dict[str, Any]) -> int | None:
    """Broad Arrow encodes ``offers.price`` as fixed-point tenths of a USD."""
    raw = offers.get("price")
    if raw is None:
        return None
    try:
        n = int(str(raw).strip().replace(",", ""))
    except ValueError:
        return None
    if n <= 0:
        return None
    return n // 10


def _mileage_from_schema(car: dict[str, Any]) -> int | None:
    odo = car.get("mileageFromOdometer")
    if not isinstance(odo, dict):
        return None
    val = odo.get("value")
    if val is None or str(val).strip() == "":
        return None
    try:
        n = int(str(val).replace(",", ""))
    except ValueError:
        return None
    unit = (odo.get("unitCode") or "").upper()
    if unit in ("KMT", "KM"):
        return round(n * 0.621371)
    return n


def parse_vehicle_detail(html: str | None, *, vehicle_path: str) -> dict[str, Any] | None:
    """Parse one vehicle detail page into a normalised dict. Returns None if no Car JSON-LD."""
    if not html or not str(html).strip():
        return None

    car: dict[str, Any] | None = None
    for m in _JSON_LD_RE.finditer(html):
        try:
            blob = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(blob, dict) and blob.get("@type") == "Car":
            car = blob
            break
    if not car:
        logger.debug("No JSON-LD Car on %s", vehicle_path)
        return None

    offers = car.get("offers") if isinstance(car.get("offers"), dict) else {}
    hammer: int | None = None
    price_span = _CONVERT_PRICE_RE.search(html)
    if price_span:
        hammer = _parse_money_us(price_span.group(1))
    if hammer is None:
        hammer = _schema_price_usd(offers)

    currency = (offers.get("priceCurrency") or "USD").strip() or "USD"
    avail = str(offers.get("availability") or "")
    sold = bool(hammer and hammer > 0)
    if "OutOfStock" in avail and hammer:
        sold = True

    year_raw = car.get("vehicleModelDate")
    year: int | None = None
    if year_raw is not None and str(year_raw).strip().isdigit():
        y = int(str(year_raw).strip())
        year = y if 1885 <= y <= 2035 else None

    make = None
    brand = car.get("brand")
    if isinstance(brand, dict) and brand.get("name"):
        make = str(brand["name"]).strip() or None
    if not make and car.get("manufacturer"):
        make = str(car["manufacturer"]).strip() or None

    model = (car.get("model") or "").strip() or None
    title = (car.get("name") or "").strip() or None
    if not title and year and make and model:
        title = f"{year} {make} {model}".strip()

    vin = (car.get("vehicleIdentificationNumber") or "").strip() or None
    vin = re.sub(r"\s+", "", vin) if vin else None
    if not vin and title:
        vin = extract_vin_from_description(title)

    eng = car.get("vehicleEngine")
    engine_spec: str | None = None
    if isinstance(eng, dict):
        parts = [
            p for p in (eng.get("name"), eng.get("engineType"), eng.get("fuelType"))
            if p and str(p).strip()
        ]
        if parts:
            raw = " ".join(str(p).strip() for p in parts)
            engine_spec = normalize_engine_spec(raw) or raw

    mileage_mi = _mileage_from_schema(car)

    lot_number: str | None = None
    auction_event: str | None = None
    lb = _LOT_BRANCH_RE.search(html)
    if lb:
        inner = html_lib.unescape(lb.group(1))
        soup_text = re.sub(r"<[^>]+>", " ", inner)
        soup_text = re.sub(r"\s+", " ", soup_text).strip()
        lm = re.search(r"Lot\s+(\d+)", soup_text, re.I)
        if lm:
            lot_number = lm.group(1)
        parts = [p.strip() for p in re.split(r"\|\s*", soup_text) if p.strip()]
        if len(parts) >= 2:
            auction_event = parts[-1].strip()
        elif parts:
            auction_event = parts[0]

    image_url: str | None = None
    img = car.get("image")
    if isinstance(img, str):
        image_url = img.strip() or None

    return {
        "record_id": vehicle_path.replace("/", "__"),
        "vehicle_path": vehicle_path,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "vin": vin,
        "mileage_mi": mileage_mi,
        "ext_color": None,
        "int_color": None,
        "engine_spec": engine_spec,
        "transmission": None,
        "lot_number": lot_number,
        "hammer": hammer,
        "currency": currency,
        "sold_status": "Sold" if sold else "Unsold",
        "selling_date": None,
        "image_url": image_url,
        "listing_url": f"{SITE_ORIGIN}/vehicles/{vehicle_path}",
        "auction_event": auction_event,
        "listing_description": None,
    }
