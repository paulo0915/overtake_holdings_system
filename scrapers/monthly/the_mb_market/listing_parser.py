"""Parse The MB Market AJAX listing HTML + listing detail pages."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import normalize_transmission

from .config import BASE_URL

_US_STATE_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
    re.IGNORECASE,
)
_ENGINE_LI_RE = re.compile(
    r"(?:\d\.?\d*\s*[Ll]iter|[Vv]\d{1,2}\b|AMG|Brabus|turbo|DOHC|SOHC|MPI|EFI|electric|battery|motor\b|flat[- ]?\d)",
    re.IGNORECASE,
)
_NO_RESERVE_PREFIX = re.compile(r"^\s*NO\s+RESERVE:\s*", re.IGNORECASE)
_PLACEHOLDER_IMG_RE = re.compile(
    r"placeholder|loading\.png|/uploads/placeholder",
    re.IGNORECASE,
)


def _is_placeholder_image_url(url: str | None) -> bool:
    if not url or not url.strip():
        return True
    return bool(_PLACEHOLDER_IMG_RE.search(url))


def _absolute_mb_url(url: str | None) -> str | None:
    """Turn site-relative or protocol-relative URLs into full ``https`` URLs."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/"):
        return f"{BASE_URL}{u}"
    return f"{BASE_URL}/{u.lstrip('/')}"


def _pick_auction_image_url(img) -> str | None:
    """Lazy cards use ``src=/uploads/placeholder-loading.png`` + real URL in ``data-src``."""
    if not img:
        return None
    src = (img.get("src") or "").strip()
    data_src = (img.get("data-src") or img.get("data-lazy-src") or "").strip()

    chosen: str | None = None
    if data_src and not _is_placeholder_image_url(data_src):
        chosen = data_src
    elif src and not _is_placeholder_image_url(src):
        chosen = src
    elif data_src:
        chosen = data_src
    elif src:
        chosen = src

    final = _absolute_mb_url(chosen)
    if not final or _is_placeholder_image_url(final):
        return None
    return final


def _parse_usd_price(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\$[\s]*([\d,]+)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def location_to_country(location: str | None) -> str | None:
    """``City, ST`` → USA / Canada when state / province is recognisable."""
    if not location:
        return None
    tail = location.split(",")[-1].strip()
    if _US_STATE_RE.search(tail):
        return "USA"
    ca_abbr = frozenset({"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"})
    if tail.upper() in ca_abbr:
        return "Canada"
    return None


def parse_listing_boxes(html: str) -> list[dict[str, Any]]:
    """Parse ``data.html`` from the AJAX JSON into one dict per auction card."""
    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict[str, Any]] = []
    for box in soup.select("div.box"):
        a = box.find("a", href=True)
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href.startswith("/"):
            continue
        slug = href.lstrip("/").strip()
        if not slug:
            continue

        h3 = a.find("h3")
        title = h3.get_text(strip=True) if h3 else ""

        label = ""
        price_txt = ""
        bid_div = a.find("div", class_="current-bid")
        if bid_div:
            spans = bid_div.find_all("span")
            if len(spans) >= 2:
                label = spans[0].get_text(strip=True).upper()
                price_txt = spans[1].get_text(strip=True)

        # Card label is either ``SOLD:`` or ``BID TO:`` (reserve not met).
        is_sold = "SOLD" in label and "BID" not in label
        is_bid_to = "BID TO" in label or ("BID" in label and "SOLD" not in label)

        img_url = _pick_auction_image_url(a.find("img"))

        out.append(
            {
                "slug": slug,
                "title": title,
                "list_label": label,
                "is_sold": is_sold,
                "is_bid_to": is_bid_to,
                "amount_usd": _parse_usd_price(price_txt),
                "image_url": img_url,
                "listing_url": f"{BASE_URL}/{slug}",
            }
        )
    return out


def parse_detail_html(html: str | None) -> dict[str, Any]:
    """Extract Quick Facts + prose + bid metadata from a listing HTML page."""
    if not html:
        return {}

    soup = BeautifulSoup(html, "lxml")
    facts: dict[str, str] = {}
    for li in soup.select("ul.dashed li"):
        raw = li.get_text(" ", strip=True)
        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        facts[k.strip().lower()] = v.strip()

    loc_span = soup.select_one("div.location span")
    location = loc_span.get_text(strip=True) if loc_span else None

    desc_parts: list[str] = []
    for p in soup.select("section.auction-listing-content section.paragraph-content p"):
        t = p.get_text(" ", strip=True)
        if t:
            desc_parts.append(t)
    description = "\n\n".join(desc_parts)[:200_000] if desc_parts else None

    bid_m = re.search(r'id="bid_history_btn"[^>]*>(\d+)</a>', html, re.I)

    end_m = re.search(
        r"ENDED\s+ON\s*</div>\s*<span>(\d{2}/\d{2}/\d{4})",
        html,
        re.I | re.DOTALL,
    )
    selling_date = None
    if end_m:
        mm, dd, yyyy = end_m.group(1).split("/")
        selling_date = f"{yyyy}-{mm}-{dd}"

    odom_raw = (facts.get("odometer") or "").replace(",", "").strip()
    mileage_mi: int | None = None
    if odom_raw.isdigit():
        mileage_mi = int(odom_raw)

    engine = None
    for li in soup.select("ul.dashed li"):
        raw = li.get_text(" ", strip=True)
        if ":" in raw:
            continue
        if _ENGINE_LI_RE.search(raw):
            engine = raw
            break

    lot_raw = facts.get("auction lot")
    lot_number: str | int | None = None
    if lot_raw:
        m = re.search(r"(\d+)", str(lot_raw))
        if m:
            lot_number = int(m.group(1))

    tx_raw = facts.get("transmission")
    transmission = normalize_transmission(tx_raw) if tx_raw else None

    return {
        "vin": facts.get("vin"),
        "mileage_mi": mileage_mi,
        "ext_color": facts.get("exterior color"),
        "int_color": facts.get("interior color"),
        "engine_spec": engine,
        "transmission": transmission,
        "lot_number": lot_number,
        "location": location,
        "country": location_to_country(location),
        "listing_description": description,
        "selling_date": selling_date,
        "chassis": facts.get("chassis"),
    }


def parse_year_make_model(title: str) -> tuple[int | None, str | None, str | None]:
    """Best-effort split from listing title."""
    t = _NO_RESERVE_PREFIX.sub("", title or "").strip()
    m = re.search(r"\b(19\d{2}|20\d{2})\b", t)
    if not m:
        return None, None, t or None
    year = int(m.group(1))
    rest = t[m.end() :].strip()
    parts = rest.split()
    if not parts:
        return year, None, None
    if len(parts) >= 2 and parts[0].lower() == "mercedes" and parts[1].lower() == "benz":
        make = "Mercedes-Benz"
        model = " ".join(parts[2:]) if len(parts) > 2 else None
        return year, make, model
    if len(parts) >= 2 and parts[0].lower() == "mercedes-benz":
        make = "Mercedes-Benz"
        model = " ".join(parts[1:])
        return year, make, model
    make = parts[0]
    model = " ".join(parts[1:]) if len(parts) > 1 else None
    return year, make, model


def merge_summary(card: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Combine AJAX card row + parsed detail page."""
    title = card.get("title") or ""
    year, make, model = parse_year_make_model(title)

    sold_status = "Sold" if card.get("is_sold") else "Unsold"
    amount = card.get("amount_usd")

    out: dict[str, Any] = {
        "record_id": card["slug"],
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "sold_status": sold_status,
        "amount_usd": amount,
        "currency": "USD",
        "image_url": card.get("image_url"),
        "listing_url": card.get("listing_url"),
    }
    if detail:
        if detail.get("vin"):
            out["vin"] = detail["vin"]
        if detail.get("mileage_mi") is not None:
            out["mileage_mi"] = detail["mileage_mi"]
        if detail.get("ext_color"):
            out["ext_color"] = detail["ext_color"]
        if detail.get("int_color"):
            out["int_color"] = detail["int_color"]
        if detail.get("engine_spec"):
            out["engine_spec"] = detail["engine_spec"]
        if detail.get("transmission"):
            out["transmission"] = detail["transmission"]
        if detail.get("lot_number") is not None:
            out["lot_number"] = detail["lot_number"]
        if detail.get("country"):
            out["country"] = detail["country"]
        if detail.get("listing_description"):
            out["listing_description"] = detail["listing_description"]
        if detail.get("bid_count") is not None:
            out["bid_count"] = detail["bid_count"]
        if detail.get("selling_date"):
            out["selling_date"] = detail["selling_date"]
    return out
