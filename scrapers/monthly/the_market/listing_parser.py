"""Parse The Market API list rows + listing detail JSON into merged summaries."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import country_code_to_name, normalize_engine_spec, normalize_mileage, normalize_transmission

from .config import CDN_ORIGIN, SITE_ORIGIN

_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def slug_part(text: str | None) -> str:
    """Lowercase slug segment for listing URLs (``/listings/{make}/{model}/{id}``)."""
    if not text:
        return "lot"
    t = text.strip().strip("'\"‘’")
    t = _SLUG_CLEAN.sub("-", t.lower()).strip("-")
    return t or "lot"


def listing_url(listing_id: str, make: str | None, model: str | None) -> str:
    return f"{SITE_ORIGIN}/listings/{slug_part(make)}/{slug_part(model)}/{listing_id}"


def listing_referer_path(listing_id: str, make: str | None, model: str | None) -> str:
    return f"/listings/{slug_part(make)}/{slug_part(model)}/{listing_id}"


def image_url_from_row(row: dict[str, Any]) -> str | None:
    """Prefer ``original_image`` (CDN path without query)."""
    path = (row.get("original_image") or row.get("image") or "").strip()
    if not path:
        return None
    path = path.split("?", 1)[0].lstrip("/")
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{CDN_ORIGIN}/{path}"


def gbp_minor_to_major_units(amount: int | None) -> int | None:
    """API amounts are in pence (e.g. ``470000`` → £4,700)."""
    if amount is None:
        return None
    try:
        return int(amount) // 100
    except (TypeError, ValueError):
        return None


def sold_status_and_amount_pence(row: dict[str, Any]) -> tuple[str, int | None]:
    """
    Derive sold / unsold from ``reserve_status`` and offline hammer fields.

    ``reserve-not-close`` with no offline sale → high bid only (unsold).
    """
    if row.get("withdrawn"):
        return "Withdrawn", None

    reserve = (row.get("reserve_status") or "").strip()
    offline = row.get("offline_sold_price")
    high = row.get("highest_bid")

    try:
        high_i = int(high) if high is not None else None
    except (TypeError, ValueError):
        high_i = None
    try:
        off_i = int(offline) if offline is not None else None
    except (TypeError, ValueError):
        off_i = None

    if reserve == "reserve-not-close" and off_i is None:
        return "Unsold", high_i

    if off_i is not None:
        return "Sold", off_i
    return "Sold", high_i


def _parse_end_date_iso(raw: str | None) -> str | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _markdown_to_text(markdown_html: str | None) -> str:
    if not markdown_html:
        return ""
    soup = BeautifulSoup(markdown_html, "lxml")
    return soup.get_text("\n", strip=True)


# Sentence must mention something transmission-related before we trust normalize_transmission
# (avoids classifying unrelated uses of “automatic”).
_TRANS_CONTEXT = re.compile(
    r"(?:gearbox|transmission|transaxle|"
    r"\b\d\s*[-]?\s*speed\b|"
    r"\b(?:three|four|five|six|seven|eight|nine|ten)\s*[-]?\s*speed\b|"
    r"\b(?:cvt|dct|pdk|smg|tiptronic)\b|"
    r"\btorque\s+converter\b)",
    re.I,
)


def _infer_speed_gearbox_sentence(text: str) -> str | None:
    """
    Phrases like “4-speed gearbox with overdrive” often omit the word “manual”;
    the API may still omit ``gearbox``. Treat as manual unless the window says automatic.
    """
    for m in re.finditer(
        r"\b(?:a\s+)?(?:\d|three|four|five|six|seven|eight)\s*[-]?\s*speed\s+gearbox\b",
        text,
        re.I,
    ):
        start = max(0, m.start() - 50)
        window = text[start : m.end() + 80].lower()
        if re.search(r"\bautomatic\b|\bauto\s+gearbox\b|\bdct\b|\bpdk\b|\bcvt\b", window):
            continue
        return "manual"
    return None


def transmission_from_description(text: str | None) -> str | None:
    """
    Fallback when ``gearbox`` is absent: scan prose for transmission cues.

    Order: (1) narrow “N-speed gearbox …” classic-lot phrasing, (2) sentences that
    mention gearbox/transmission/speed etc., passed through ``normalize_transmission``.
    """
    if not text or not str(text).strip():
        return None
    body = str(text).strip()

    inferred = _infer_speed_gearbox_sentence(body)
    if inferred:
        return inferred

    parts = re.split(r"(?<=[.!?])\s+|(?:\n\s*){2,}", body)
    for raw in parts:
        chunk = raw.strip()
        if len(chunk) < 10:
            continue
        if not _TRANS_CONTEXT.search(chunk):
            continue
        n = normalize_transmission(chunk)
        if n:
            return n
    return None


def parse_list_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one element from ``/api/listings/results`` ``data`` array."""
    lid = str(row.get("id") or "").strip()
    make = (row.get("make") or "").strip().strip("'\"‘’") or None
    model = (row.get("model") or "").strip() or None
    year_s = row.get("year")
    year: int | None = None
    if year_s is not None and str(year_s).strip().isdigit():
        year = int(str(year_s).strip())

    sold_status, amt_pence = sold_status_and_amount_pence(row)
    title_parts = [str(p) for p in (year, make, model) if p]
    title = " ".join(title_parts).strip() or (row.get("tagline") or "").strip() or lid

    return {
        "record_id": lid,
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "sold_status": sold_status,
        "amount_gbp_major": gbp_minor_to_major_units(amt_pence),
        "currency": "GBP",
        "bid_count": row.get("bids_count"),
        "selling_date": _parse_end_date_iso(row.get("end_date")),
        "lot_number": row.get("lot_number"),
        "image_url": image_url_from_row(row),
        "listing_url": listing_url(lid, make, model),
        "referer_path": listing_referer_path(lid, make, model),
        "country": country_code_to_name(row.get("location_country")),
        "tagline": row.get("tagline"),
    }


def parse_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Fields from ``GET /api/listings/{id}`` (merged over list row)."""
    if not detail:
        return {}

    out: dict[str, Any] = {}

    mileage_mi: int | None = None
    raw_mileage = detail.get("mileage")
    if isinstance(raw_mileage, str) and raw_mileage.strip():
        rl = raw_mileage.strip().lower()
        unit = "km" if "km" in rl or "kilomet" in rl else None
        mileage_mi = normalize_mileage(raw_mileage, unit)

    desc_parts: list[str] = []
    if detail.get("summary"):
        desc_parts.append(str(detail["summary"]).strip())
    if detail.get("quote"):
        desc_parts.append(str(detail["quote"]).strip())
    content = detail.get("content")
    if isinstance(content, list):
        blocks = sorted(content, key=lambda c: (c.get("order") if isinstance(c, dict) else 0, str(c.get("title"))))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            md = block.get("markdown")
            if md:
                txt = _markdown_to_text(str(md))
                if txt:
                    desc_parts.append(txt)
    facts = detail.get("keyFacts")
    if isinstance(facts, list):
        lines = [str(f.get("fact_text", "")).strip() for f in facts if isinstance(f, dict)]
        lines = [x for x in lines if x]
        if lines:
            desc_parts.append("Key facts: " + "; ".join(lines))

    description = "\n\n".join(desc_parts)[:200_000] if desc_parts else None

    tx = normalize_transmission(detail.get("gearbox"))
    if not tx and description:
        tx = transmission_from_description(description)
    engine = normalize_engine_spec(detail.get("engine"))

    est_low = gbp_minor_to_major_units(detail.get("lower_estimate_price"))
    est_high = gbp_minor_to_major_units(detail.get("higher_estimate_price"))
    est_cur = "GBP" if detail.get("display_estimates") else None
    if not detail.get("display_estimates"):
        est_low = est_high = None
        est_cur = None

    vin = (detail.get("chassis") or "").strip() or None

    out.update(
        {
            "vin": vin,
            "mileage_mi": mileage_mi,
            "ext_color": (detail.get("color") or "").strip() or None,
            "int_color": (detail.get("interior") or "").strip() or None,
            "engine_spec": engine,
            "transmission": tx,
            "listing_description": description,
            "estimate_low": est_low,
            "estimate_high": est_high,
            "estimate_currency": est_cur,
            "country": country_code_to_name(
                detail.get("location_country") or detail.get("country_of_registration")
            ),
        }
    )
    return out


def merge_summary(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    out = dict(summary)
    d = parse_detail(detail)
    for k, v in d.items():
        if v is not None and v != "":
            out[k] = v
    if detail and detail.get("withdrawn"):
        out["sold_status"] = "Withdrawn"
        out["amount_gbp_major"] = None
    if not out.get("transmission"):
        hint = "\n\n".join(
            x for x in (out.get("tagline"), out.get("listing_description")) if x
        )
        tx2 = transmission_from_description(hint)
        if tx2:
            out["transmission"] = tx2
    return out
