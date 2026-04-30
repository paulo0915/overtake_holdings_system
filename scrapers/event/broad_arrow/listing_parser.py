"""Parse Broad Arrow HTML pages — past-auctions index, event pages, results listing, vehicle detail."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from shared.normalizer import normalize_engine_spec

from scrapers.event.mecum import extract_vin_from_description

from .config import RESULTS_PATH, SITE_ORIGIN, SORT_PARAM, SORT_VALUE

logger = logging.getLogger(__name__)

# --- Regexes -----------------------------------------------------------------

_EVENT_PATH_RE = re.compile(r'href=["\'](/events/event/[^"\']+)["\']', re.IGNORECASE)

# Extract event slug from a vehicle path or URL: "ve26_r0025/some-title" → "ve26"
_EVENT_SLUG_RE = re.compile(r"(?:vehicles/)?([a-z0-9]+)_r\d+", re.IGNORECASE)

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
# Full convert_price block: captures one amount (sold) OR ``$X,XXX - $Y,YYY`` (upcoming estimate).
_CONVERT_PRICE_FULL_RE = re.compile(
    r'<span[^>]*id=[\'"]convert_price[\'"][^>]*>([^<]+)</span>',
    re.IGNORECASE,
)
_PRICE_LABEL_RE = re.compile(
    r'<span[^>]*id=[\'"]label[\'"][^>]*>\s*([^<]+?)\s*</span>',
    re.IGNORECASE,
)
# Reserve state: ``Offered Without Reserve`` | ``Reserve Met`` | ``Pending Reserve``.
_RESERVE_RE = re.compile(
    r'<div[^>]*class=[\'"]reserve[^\'"]*[\'"][^>]*>(.*?)</div>',
    re.S | re.IGNORECASE,
)
# Vehicle description + highlights blocks.
_DESCRIPTION_RE = re.compile(
    r'<(div|section|article)[^>]*class=["\'][^"\']*about-vehicle-section[^"\']*["\'][^>]*>(.*?)</\1>',
    re.S | re.IGNORECASE,
)
_HIGHLIGHTS_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*highlights-content[^"\']*["\'][^>]*>(.*?)</div>',
    re.S | re.IGNORECASE,
)
# ``<div class='price-row …'>`` wraps either the estimate block or a bare status
# span like ``<span>Sold Before Auction</span>``. Capture the price-row body.
_PRICE_ROW_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*price-row[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
    re.S | re.IGNORECASE,
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


def extract_event_slug(vehicle_path: str) -> str | None:
    """Extract the event slug from a vehicle path or URL.

    Examples:
        "ve26_r0025/2024-porsche-..."  → "ve26"
        "/vehicles/po26_r0010/1970-..."  → "po26"
        "gi26v_r0003/..."  → "gi26v"
    """
    m = _EVENT_SLUG_RE.search(vehicle_path or "")
    return m.group(1).lower() if m else None


# Location keywords in priority order: (keyword_in_lowercase, country, currency)
_LOC_CURRENCY_HINTS: list[tuple[str, str, str]] = [
    # Italian cities / venues
    ("cernobbio", "Italy", "EUR"),
    ("villa erba", "Italy", "EUR"),
    ("villa d'este", "Italy", "EUR"),
    ("villa este", "Italy", "EUR"),
    ("maranello", "Italy", "EUR"),
    ("milan", "Italy", "EUR"),
    (" italy", "Italy", "EUR"),
    ("italian", "Italy", "EUR"),
    # Monaco
    ("monte carlo", "Monaco", "EUR"),
    (" monaco", "Monaco", "EUR"),
    # France
    ("paris", "France", "EUR"),
    ("le mans", "France", "EUR"),
    (" france", "France", "EUR"),
    # Germany
    ("munich", "Germany", "EUR"),
    ("stuttgart", "Germany", "EUR"),
    (" germany", "Germany", "EUR"),
    # Switzerland
    ("geneva", "Switzerland", "CHF"),
    ("zurich", "Switzerland", "CHF"),
    ("switzerland", "Switzerland", "CHF"),
    # UK
    ("goodwood", "UK", "GBP"),
    ("london", "UK", "GBP"),
    ("newport pagnell", "UK", "GBP"),
    ("united kingdom", "UK", "GBP"),
    ("england", "UK", "GBP"),
    (" uk", "UK", "GBP"),
    # USA cities
    ("costa mesa", "USA", "USD"),
    ("scottsdale", "USA", "USD"),
    ("monterey", "USA", "USD"),
    ("pebble beach", "USA", "USD"),
    ("amelia island", "USA", "USD"),
    ("greenwich", "USA", "USD"),
    ("las vegas", "USA", "USD"),
    ("california", "USA", "USD"),
    ("united states", "USA", "USD"),
    (" usa", "USA", "USD"),
    # Other
    ("canada", "Canada", "CAD"),
    ("japan", "Japan", "JPY"),
    ("australia", "Australia", "AUD"),
    ("dubai", "UAE", "USD"),
]

_MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})(?:[–\-]\d{{1,2}})?\s+({_MONTHS})\s+(\d{{4}})\b",
    re.IGNORECASE,
)


def parse_event_meta_from_html(html: str | None) -> dict[str, str | None]:
    """Extract event metadata from a Broad Arrow event page.

    Returns ``{"country": ..., "currency": ..., "date": "YYYY-MM-DD" | None}``.
    Falls back to ``{"country": None, "currency": None, "date": None}`` on failure.
    """
    meta: dict[str, str | None] = {"country": None, "currency": None, "date": None}
    if not html:
        return meta

    # Strip tags and collapse whitespace for text search.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", html_lib.unescape(text)).strip()
    text_lower = text.lower()

    for keyword, country, currency in _LOC_CURRENCY_HINTS:
        if keyword in text_lower:
            meta["country"] = country
            meta["currency"] = currency
            break

    date_m = _DATE_RE.search(text)
    if date_m:
        try:
            dt = datetime.strptime(
                f"{date_m.group(1)} {date_m.group(2)} {date_m.group(3)}", "%d %B %Y"
            )
            meta["date"] = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return meta


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


def _extract_upcoming_status_text(html: str) -> str | None:
    """When the price-row has no ``convert_price`` span, return its bare status text.

    Real examples: ``Sold Before Auction``, ``Post-Auction Sale``, ``Withdrawn``.
    Returns ``None`` if a normal estimate/price block is present (caller handles that).
    """
    for m in _PRICE_ROW_RE.finditer(html):
        inner = m.group(1)
        if "convert_price" in inner or "id='label'" in inner or 'id="label"' in inner:
            continue
        text = _html_to_text(inner)
        text = re.sub(r"\bUSD|\bEUR|\bGBP|\bCHF", "", text).strip()
        if text and len(text) < 60:
            return text
    return None


def _parse_convert_price_block(html: str) -> tuple[int | None, int | None, str | None]:
    """Return ``(low_or_hammer, high_or_none, label)``.

    The ``convert_price`` span holds either ``$X,XXX`` (hammer for sold lots) or
    ``$X,XXX - $Y,YYY`` (estimate range for upcoming). The adjacent ``label`` span
    says ``Sold for`` / ``Estimate:`` / ``Bid to``.
    """
    label_m = _PRICE_LABEL_RE.search(html)
    label = (html_lib.unescape(label_m.group(1)).strip().rstrip(":")
             if label_m else None)
    price_m = _CONVERT_PRICE_FULL_RE.search(html)
    if not price_m:
        return None, None, label
    inner = html_lib.unescape(price_m.group(1)).strip()
    amounts = [int(x.replace(",", "")) for x in re.findall(r"\$([\d,]+)", inner)]
    if not amounts:
        return None, None, label
    if len(amounts) >= 2:
        return amounts[0], amounts[1], label
    return amounts[0], None, label


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"</li>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text).strip()
    return text


def _extract_description(html: str) -> str | None:
    """Return the ``about-vehicle-section`` + ``highlights-content`` combined as text."""
    if not html:
        return None
    parts: list[str] = []
    hl = _HIGHLIGHTS_RE.search(html)
    if hl:
        txt = _html_to_text(hl.group(1))
        if txt:
            parts.append(f"Highlights:\n{txt}")
    desc = _DESCRIPTION_RE.search(html)
    if desc:
        txt = _html_to_text(desc.group(2))
        if txt:
            parts.append(txt)
    return "\n\n".join(parts) if parts else None


def _extract_reserve(html: str) -> str | None:
    """Return ``Without Reserve`` / ``Reserve Met`` etc from the reserve div."""
    if not html:
        return None
    for m in _RESERVE_RE.finditer(html):
        inner = m.group(1)
        text = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner))).strip()
        text = text.lstrip("|").strip()
        if text:
            return text
    return None


def _split_lot_and_branch(text: str) -> tuple[str | None, str | None]:
    """``Lot 201 | The Porsche Air|Water Auction 2026`` → (``201``, ``The Porsche Air|Water Auction 2026``).

    Splits on the first pipe only — some auction names themselves contain pipes.
    """
    lm = re.search(r"Lot\s+([A-Za-z0-9\-]+)", text, re.I)
    lot = lm.group(1) if lm else None
    parts = text.split("|", 1)
    auction = parts[1].strip() if len(parts) > 1 else None
    return lot, auction


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


def parse_vehicle_detail(
    html: str | None, *, vehicle_path: str, mode: str = "past",
) -> dict[str, Any] | None:
    """Parse one vehicle detail page into a normalised dict. Returns None if no Car JSON-LD.

    ``mode='past'``    → treat ``convert_price`` as hammer (existing behaviour).
    ``mode='upcoming'`` → treat ``convert_price`` as estimate range and skip hammer.
    """
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
    avail = str(offers.get("availability") or "")
    currency = (offers.get("priceCurrency") or "USD").strip() or "USD"

    price_low, price_high, price_label = _parse_convert_price_block(html)
    label_lower = (price_label or "").lower()

    hammer: int | None = None
    estimate_low: int | None = None
    estimate_high: int | None = None
    sold = False

    upcoming_status: str | None = None
    if mode == "upcoming":
        # Upcoming: ``convert_price`` holds ``$low - $high`` estimate range, no hammer.
        estimate_low = price_low
        estimate_high = price_high
        if estimate_low is None and estimate_high is None:
            # No estimate block — this lot likely shows a status banner instead
            # (e.g. "Sold Before Auction", "Post-Auction Sale", "Withdrawn").
            upcoming_status = _extract_upcoming_status_text(html)
    else:
        # Past: prefer the ``convert_price`` span single-amount as hammer.
        if price_low is not None and price_high is None and "estimate" not in label_lower:
            hammer = price_low
        elif price_low is not None and price_high is not None and "estimate" not in label_lower:
            # Two amounts with a non-estimate label — rare, but keep the first as hammer.
            hammer = price_low
        elif price_low is not None and price_high is not None and "estimate" in label_lower:
            # Estimate range on a past lot that didn't sell — record as estimate.
            estimate_low = price_low
            estimate_high = price_high
        if hammer is None:
            hammer = _schema_price_usd(offers)
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
        lot_number, auction_event = _split_lot_and_branch(soup_text)

    image_url: str | None = None
    img = car.get("image")
    if isinstance(img, str):
        image_url = img.strip() or None

    reserve = _extract_reserve(html)
    description = _extract_description(html)

    if mode == "upcoming":
        # Default: empty for live-upcoming lots. If a banner like "Sold Before
        # Auction" is present, surface that as the status instead.
        sold_status = upcoming_status if upcoming_status else None
    else:
        sold_status = "Sold" if sold else "Unsold"

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
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "reserve": reserve,
        "currency": currency,
        "sold_status": sold_status,
        "selling_date": None,
        "image_url": image_url,
        "listing_url": f"{SITE_ORIGIN}/vehicles/{vehicle_path}",
        "auction_event": auction_event,
        "listing_description": description,
    }
