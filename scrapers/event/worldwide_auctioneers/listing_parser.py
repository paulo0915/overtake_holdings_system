"""Parse Worldwide Auctioneers — search form defaults + POST car-search result blocks.

POST ``/car-search/`` with ``auctionid=0 / make=All`` returns ~5 MB of HTML
containing every historical lot as a ``div.col.span_12[style*=margin]`` block.
We split on that marker (fast string split, no slow DOTALL regex on 5 MB) and
parse each block for: auction name, lot number, title, sold status, price, image.

VINs are **NOT** present in the POST response — they only appear on individual
``/product/{slug}/`` pages inside ``<h4>VIN: XXXXXXX</h4>``.  Fetching every
product page for 10,000+ lots would take hours, so VIN is left empty by default.
If you need VINs, run the scraper with ``--fetch-details`` (future enhancement)
or enrich from a separate product-page pass.

Non-car lots (memorabilia, signs, artwork, etc.) are filtered out by requiring
the title to start with a 4-digit year (1885–2035).
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from .config import CAR_SEARCH_ALL_LISTINGS_BODY, LISTINGS_ORIGIN

# ---------------------------------------------------------------------------
# Regexes — all scoped to the first ~2 000 chars of each block (fast)
# ---------------------------------------------------------------------------

_LOT_RE = re.compile(r'<h4[^>]*>Lot\s+(\d+)</h4>', re.I)
_TITLE_RE = re.compile(r'<h3[^>]*>(.*?)</h[36]>', re.I)   # site typo: closes with </h6>
_SPAN8_RE = re.compile(r'<div class="col span_8">(.*?)$', re.I | re.S)
_AUC_RE = re.compile(r'^([^<\n]{5,100}?)\s*<br', re.I)
_STATUS_RE = re.compile(
    r'<font[^>]*>\s*(SOLD|UNSOLD|NOT SOLD|PASS(?:ED)?|WITHDRAWN|BID TO)[^<]*</font>',
    re.I,
)
_PRICE_H4_RE = re.compile(r'<h4[^>]*>\s*\$([\d,]+)\s*</h4>', re.I)
_PRICE_DISCLOSED_RE = re.compile(
    r'<h4[^>]*>\s*(Undisclosed|Disclosed|Not Disclosed|TBD|N/A|Contact Us)\s*</h4>',
    re.I,
)
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)

# Only accept titles that start with a model year: 1885..2035
_YEAR_TITLE_RE = re.compile(r'^(1[89]\d{2}|20[0-2]\d|2030|2031|2032|2033|2034|2035)\s+(.+)$')

_BLOCK_MARKER = '<div class="col span_12" style="margin:1rem 0;">'

# ---------------------------------------------------------------------------
# POST form body
# ---------------------------------------------------------------------------


def car_search_post_body() -> dict[str, str]:
    """Canonical 'all listings' POST body (matches DevTools payload exactly)."""
    return dict(CAR_SEARCH_ALL_LISTINGS_BODY)


# ---------------------------------------------------------------------------
# POST response parser
# ---------------------------------------------------------------------------


def _parse_year_make_model(title: str) -> tuple[int, str | None, str | None]:
    """Extract (year, make, model) from a title like ``1929 Duesenberg Model J Berline``."""
    m = _YEAR_TITLE_RE.match(title)
    if not m:
        return 0, None, title
    year = int(m.group(1))
    rest = m.group(2).strip()
    parts = rest.split(None, 1)
    if len(parts) == 2:
        return year, parts[0], parts[1]
    if len(parts) == 1:
        return year, parts[0], None
    return year, None, None


def parse_car_search_results(html: str) -> list[dict[str, Any]]:
    """Split POST response on block marker; parse each lot; filter non-car rows.

    Returns a list of dicts ready for ``record_builder.build_records``.
    Non-car lots (memorabilia, signs, artwork) are dropped by the year-prefix rule.
    """
    parts = html.split(_BLOCK_MARKER)
    out: list[dict[str, Any]] = []
    non_car = 0
    skipped = 0

    for raw in parts[1:]:  # first chunk is the page header
        scope = raw[:2000]  # all relevant data is within first ~1 kb

        lot_m = _LOT_RE.search(scope)
        if not lot_m:
            skipped += 1
            continue
        lot = lot_m.group(1)

        title_m = _TITLE_RE.search(scope)
        if not title_m:
            skipped += 1
            continue
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
        title = html_lib.unescape(title)
        if not title:
            skipped += 1
            continue

        # filter non-car lots
        if not _YEAR_TITLE_RE.match(title):
            non_car += 1
            continue

        year, make, model = _parse_year_make_model(title)

        # auction name is plain text inside .col.span_8 before first <br
        auction = ""
        s8 = _SPAN8_RE.search(scope)
        if s8:
            am = _AUC_RE.search(s8.group(1))
            if am:
                auction = html_lib.unescape(am.group(1).strip())

        status_m = _STATUS_RE.search(scope)
        status_raw = status_m.group(1).strip().upper() if status_m else "SOLD"
        sold = status_raw == "SOLD"

        price_m = _PRICE_H4_RE.search(scope)
        price: int | None = None
        price_disclosed = False
        if price_m:
            price = int(price_m.group(1).replace(",", ""))
        else:
            disc_m = _PRICE_DISCLOSED_RE.search(scope)
            if disc_m:
                price_disclosed = True

        img_m = _IMG_RE.search(scope)
        img_src = img_m.group(1) if img_m else None
        if img_src and img_src.startswith("/"):
            img_src = LISTINGS_ORIGIN + img_src

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        record_id = f"ww_{lot}_{slug[:60]}"

        out.append({
            "record_id": record_id,
            "lot": lot,
            "title": title,
            "year": year,
            "make": make,
            "model": model,
            "auction_event": auction,
            "sold": sold,
            "price": price,
            "price_disclosed": price_disclosed,
            "currency": "USD",
            "image_url": img_src,
            # VIN not available in POST response — would require per-product page fetch
            "vin": None,
            "mileage_mi": None,
        })

    return out
