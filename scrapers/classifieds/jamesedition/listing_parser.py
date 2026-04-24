"""Parse JamesEdition list + detail HTML."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from shared.normalizer import (
    KM_TO_MILES,
    canonical_country_name,
    normalize_engine_spec,
    normalize_transmission,
    parse_year_from_title,
)

from .config import SITE_ORIGIN

logger = logging.getLogger(__name__)


# --- List parsing ------------------------------------------------------------

# ``<div class="ListingCard" data-id="..." data-price-usd="..." ...>``
# — opening tag only. We capture the attribute blob and the tag's bounds so
# we can pull additional info (title + location + image) from the nested HTML.
_LISTING_CARD_OPEN_RE = re.compile(
    r'<div class="ListingCard"\s+(data-id="\d+"[^>]*?)>',
    re.I,
)

_DATA_ATTR_RE = re.compile(r'data-([\w-]+)="([^"]*)"')

# The wrapping anchor carries the canonical detail URL + title attribute.
_LISTING_ANCHOR_RE = re.compile(
    r'<a\s+href="(/cars/[^"]+?-for-sale-\d+)"\s+title="([^"]*)"',
    re.I,
)

# Hero photo: first ``<img src>`` in the card for non-lazy cards, else the
# first ``data-src`` on a ``je2-lazy-load`` div. Lazy-loaded cards dominate
# beyond the first 1–2 tiles so both paths must succeed.
_HERO_IMG_RE = re.compile(r'<img\s+src="([^"]+\.(?:jpg|webp|jpeg|png))"', re.I)
_LAZY_IMG_RE = re.compile(
    r'<div class="je2-lazy-load"[^>]+data-src="([^"]+\.(?:jpg|webp|jpeg|png))"',
    re.I,
)

# ``ListingCard__title`` div carries the location line ("Beverly Hills, CA, United States").
_LOCATION_RE = re.compile(
    r'<div class="ListingCard__title">\s*([^<]+?)\s*</div>', re.I,
)

# Listing id lives in the URL tail: ``-for-sale-17738940``.
_ID_FROM_URL_RE = re.compile(r"-for-sale-(\d+)\b")


def _parse_data_attrs(blob: str) -> dict[str, str]:
    """Turn ``data-id="17" data-price-usd="176990" ...`` → dict."""
    return {k: html_lib.unescape(v) for k, v in _DATA_ATTR_RE.findall(blob)}


def iter_listings(html: str):
    """Yield one dict per ``ListingCard`` in the SRP HTML.

    Each yielded dict carries:
      * everything in ``data-*`` on the ``<div class="ListingCard">``
      * ``listing_url`` / ``listing_title`` from the wrapping ``<a>``
      * ``image_url`` from the first ``<img src>``
      * ``location`` from the nested ``ListingCard__title`` div
      * ``listing_id`` parsed from the URL tail
    """
    for match in _LISTING_CARD_OPEN_RE.finditer(html):
        attrs = _parse_data_attrs(match.group(1))
        # Grab the slice from this card's opening tag to the next card (or
        # end of list). Keeps per-card sub-regex scans local.
        start = match.start()
        next_open = _LISTING_CARD_OPEN_RE.search(html, match.end())
        end = next_open.start() if next_open else (start + 15_000)
        slice_html = html[start:end]

        anchor = _LISTING_ANCHOR_RE.search(slice_html)
        listing_url = None
        listing_title = None
        if anchor:
            listing_url = f"{SITE_ORIGIN}{anchor.group(1)}"
            listing_title = html_lib.unescape(anchor.group(2))

        image_match = _HERO_IMG_RE.search(slice_html) or _LAZY_IMG_RE.search(slice_html)
        image_url = image_match.group(1) if image_match else None

        loc_match = _LOCATION_RE.search(slice_html)
        location = loc_match.group(1).strip() if loc_match else None

        listing_id = None
        if listing_url:
            m = _ID_FROM_URL_RE.search(listing_url)
            if m:
                listing_id = m.group(1)
        if not listing_id and attrs.get("id"):
            listing_id = attrs["id"]

        yield {
            "listing_id": listing_id,
            "listing_url": listing_url,
            "listing_title": listing_title,
            "image_url": image_url,
            "location_text": location,
            **attrs,
        }


def parse_list_tile(tile: dict[str, str | None]) -> dict[str, Any] | None:
    """Flatten one ``iter_listings`` dict into our intermediate record shape.

    Returns ``None`` if the tile lacks a ``listing_id`` + ``listing_url`` —
    we need both to scrape detail and dedupe.
    """
    listing_id = tile.get("listing_id")
    listing_url = tile.get("listing_url")
    if not listing_id or not listing_url:
        return None

    # ``data-price-usd`` is always set (site pre-converts for display);
    # ``data-price`` + ``data-currency`` give the source price. We store USD
    # as ``Asking_price_USD`` directly and keep the source for ``Asking_price``.
    try:
        price_usd = int(tile.get("price-usd") or 0) or None
    except (TypeError, ValueError):
        price_usd = None
    try:
        price_src = int(tile.get("price") or 0) or None
    except (TypeError, ValueError):
        price_src = None
    currency = (tile.get("currency") or "USD").upper()

    make = tile.get("brand") or None
    model = tile.get("model") or None
    country = canonical_country_name(tile.get("country"))
    city = (tile.get("city") or "").strip() or None
    # JE's city attribute is lowercased; ``country-subdivision`` is the
    # state/province in human form, so prefer that for city display when
    # available. Fall back to the ``location_text`` line we parsed.
    city_display = (tile.get("country-subdivision") or "").strip() or city

    title = tile.get("listing_title")
    year = parse_year_from_title(title or "")

    return {
        "record_id": f"je_{listing_id}",
        "listing_id": listing_id,
        "listing_url": listing_url,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "city": city_display,
        "country": country or "USA",
        "asking_price_src": price_src,
        "asking_price_usd_from_list": price_usd,
        "currency": currency if currency else "USD",
        "image_url": tile.get("image_url"),
        "location_text": tile.get("location_text"),
    }


# --- Detail parsing ----------------------------------------------------------

# Detail page structured-fields dl: ``<span>Key</span><p>Value</p>`` pairs
# inside ``je2-listing-details-table``. Capture them all.
_DETAILS_TABLE_RE = re.compile(
    r'<div class="je2-listing-details-table">(.+?)</div>',
    re.S | re.I,
)
_DETAILS_ROW_RE = re.compile(
    r'<span>\s*([^<]+?)\s*</span>\s*<p>\s*([^<]+?)\s*</p>',
    re.I,
)

# JSON-LD ``@type: Product`` block — source of dealer name.
_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">\s*({[^<]*?"@type"\s*:\s*"Product"[^<]*})\s*</script>',
    re.S,
)

# "About This Car" dealer description.
#
# JE's markup:
#     <h2>About This Car</h2>
#       <div class="je2-listing-about js-listing-about">
#         <div class="je2-read-more js-read-more _original" data-block="">
#           <div class="je2-read-more__preview _translated"></div>
#           <div class="je2-read-more__preview _original">…prose…</div>
#         </div>
#       </div>
#
# We match the ``_original`` preview directly — a two-step
# section→body chain breaks because the non-greedy section regex eats
# ``</div>`` pairs that the body needs to terminate on. Direct match
# works because ``_original`` classes don't occur elsewhere on the VDP.
_ABOUT_BODY_RE = re.compile(
    r'<div[^>]*class="je2-read-more__preview\s+_original"[^>]*>(.*?)</div>',
    re.S | re.I,
)

# Optional gate: only treat the match as the description when it appears
# after the "About This Car" heading — prevents stray ``_original`` divs
# elsewhere on the page from being picked up (none exist today, but the
# guard keeps us honest if JE reuses the class name later).
_ABOUT_HEADING_RE = re.compile(r'<h2>About This Car</h2>', re.I)


# HTML → plain text (preserving paragraph structure).
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_LI_RE = re.compile(r"</li\s*>", re.I)
_P_CLOSE_RE = re.compile(r"</p\s*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_COLLAPSE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _BR_RE.sub("\n", raw)
    text = _LI_RE.sub("\n", text)
    text = _P_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = [_WS_COLLAPSE_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = _MULTI_BLANK_RE.sub("\n\n", text).strip()
    return text or None


# Mileage is sometimes ``"82K Mi"`` in specs, sometimes ``"82079 mi (132093 km)"``
# in the full table. Prefer the full-table form (digits + ``mi``).
_MILEAGE_MI_RE = re.compile(r"(\d[\d,]*)\s*mi\b", re.I)
_MILEAGE_KM_RE = re.compile(r"(\d[\d,]*)\s*km\b", re.I)


def _parse_mileage(raw: str | None) -> int | None:
    if not raw:
        return None
    mi = _MILEAGE_MI_RE.search(raw)
    if mi:
        try:
            return int(mi.group(1).replace(",", ""))
        except (TypeError, ValueError):
            pass
    km = _MILEAGE_KM_RE.search(raw)
    if km:
        try:
            km_val = int(km.group(1).replace(",", ""))
            return round(km_val * KM_TO_MILES)
        except (TypeError, ValueError):
            pass
    return None


def _parse_details_table(html: str) -> dict[str, str]:
    """Return the ``<span>Key</span><p>Value</p>`` dict from the details table."""
    block = _DETAILS_TABLE_RE.search(html)
    if not block:
        return {}
    return {
        html_lib.unescape(k).strip(): html_lib.unescape(v).strip()
        for k, v in _DETAILS_ROW_RE.findall(block.group(1))
    }


def _extract_dealer_name(html: str) -> str | None:
    m = _JSONLD_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    offers = data.get("offers") or {}
    seller = offers.get("seller") or {}
    name = (seller.get("name") or "").strip() or None
    return name


def _extract_description(html: str) -> str | None:
    """Pull dealer-written "About This Car" body.

    Require the About heading to be present on the page (sanity gate),
    then take the first ``_original`` preview that follows. An empty
    body (dealer didn't write one) returns ``None``.
    """
    about_m = _ABOUT_HEADING_RE.search(html)
    if not about_m:
        return None
    # Search only the slice after the heading so an unrelated preview
    # elsewhere on the page can't be picked up by mistake.
    body = _ABOUT_BODY_RE.search(html, about_m.end())
    if not body:
        return None
    raw = body.group(1).strip()
    if not raw:
        return None
    cleaned = _html_to_text(raw)
    return cleaned or None


def parse_detail_html(html: str) -> dict[str, Any]:
    """Pull VIN / colours / engine / gearbox / mileage / dealer / description
    out of a JamesEdition detail page.

    Missing fields come back as ``None``; dealers often leave the
    description blank, and a fair chunk of listings have no VIN.
    """
    if not html:
        return {}

    dt = _parse_details_table(html)

    vin = (dt.get("VIN") or "").strip() or None
    ext_color = (dt.get("Color") or "").strip() or None
    int_color = (dt.get("Interior color") or "").strip() or None

    # Year — the list-tile title often lacks a leading year for aircraft /
    # some dealer-entered Bugattis; the details table fills it in.
    year = None
    year_raw = (dt.get("Year") or "").strip()
    if year_raw:
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            year = None

    engine_raw = dt.get("Engine")
    engine_spec = normalize_engine_spec(engine_raw) if engine_raw else None
    # Bare cylinder counts (``"6 Cylinder"``) get rejected by the normalizer.
    # Fall back to the raw string — dealers don't give displacement on JE.
    if not engine_spec and engine_raw:
        engine_spec = engine_raw.strip() or None

    transmission = normalize_transmission(dt.get("Gearbox"))
    mileage_mi = _parse_mileage(dt.get("Mileage"))

    dealer_name = _extract_dealer_name(html)
    description = _extract_description(html)

    return {
        "year": year,
        "vin": vin,
        "ext_color": ext_color,
        "int_color": int_color,
        "engine_spec": engine_spec,
        "transmission": transmission,
        "mileage_mi": mileage_mi,
        "dealer_name": dealer_name,
        "description": description,
        # Also carry the full details-table city/location in case the list
        # line was truncated.
        "location_text": dt.get("Location"),
    }
