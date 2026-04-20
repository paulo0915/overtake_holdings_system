"""Parse DuPont Live API rows + optional __NEXT_DATA__ pageProps enrichment."""

from __future__ import annotations

import html
import re
from typing import Any

from shared.normalizer import extract_description_fields, normalize_mileage, normalize_transmission

from .config import BASE_URL

_US_STATE_RE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
    re.IGNORECASE,
)
_CA_PROVINCE_ABBR = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
})
_CA_PROVINCE_NAMES = frozenset({
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "northwest territories",
    "nunavut", "ontario", "prince edward island", "quebec", "saskatchewan", "yukon",
})
_VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
# DuPont sometimes stores the engine line in ``vehicleData.driveTrain`` (wrong field).
_DT_ENGINE_HINT = re.compile(
    r"(?:\d(?:\.\d+)?\s*[- ]?\s*(?:liter|litre)\b)|\bcylinder\b|\bflat\s*\d|\bv\d{1,2}\b|\bw\d{1,2}\b|"
    r"quad[- ]?turbo|twin[- ]?turbo|fuel\s+inject|injected|mpi|efi",
    re.IGNORECASE,
)


def _drive_train_looks_like_engine_spec(s: str) -> bool:
    return bool(_DT_ENGINE_HINT.search(s))


def _html_to_plain(s: str) -> str:
    t = html.unescape(s)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_vin(text: str) -> str | None:
    m = _VIN_RE.search(text.upper())
    return m.group(1) if m else None


def _mileage_from_dupont_model(model: str) -> int | None:
    """
    DuPont model strings often look like ``'… | 1-of-60, ~7,600 Miles'``.
    Only parse segments that explicitly mention miles/mi (avoid ``1-of-60``, ``16.4``, etc.).
    """
    if not model or not re.search(r"\bmiles?\b|\bmi\b", model, re.IGNORECASE):
        return None
    best: int | None = None
    for m in re.finditer(
        r"(?:~|approx\.?\s*)?([\d,]+(?:\.\d+)?)\s*(k)?\s*(?:miles?\b|\bmi\b)",
        model,
        re.IGNORECASE,
    ):
        frag = m.group(0)
        val = normalize_mileage(frag, "mi")
        if val is not None:
            best = val
    return best


def location_to_country(city: str | None, state: str | None) -> str | None:
    """Infer country from city/state when present.

    Checks US states first (regex), then Canadian province abbreviations and
    full names. The ``len(st) == 2`` wildcard is intentionally absent — it
    would misclassify Canadian two-letter abbreviations (ON, BC, QC…) as USA.
    """
    st = (state or "").strip()
    if not st:
        return None
    if _US_STATE_RE.search(st):
        return "USA"
    if st.upper() in _CA_PROVINCE_ABBR or st.lower() in _CA_PROVINCE_NAMES:
        return "Canada"
    return None


def _deep_find_auction_by_id(obj: Any, auction_id: str) -> dict[str, Any] | None:
    """Depth-first search for a dict whose auctionId matches (SSR may nest auction elsewhere)."""
    if isinstance(obj, dict):
        if str(obj.get("auctionId") or "").upper() == auction_id.upper():
            return obj
        for v in obj.values():
            found = _deep_find_auction_by_id(v, auction_id)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_auction_by_id(item, auction_id)
            if found:
                return found
    return None


def _title_image_description(vd: dict[str, Any]) -> str | None:
    files = vd.get("files") or {}
    if not isinstance(files, dict):
        return None
    ti = files.get("titleImage") or {}
    if not isinstance(ti, dict):
        return None
    desc = ti.get("description")
    return desc if isinstance(desc, str) and desc.strip() else None


def pick_enriched_auction(
    page_props: dict[str, Any] | None,
    *,
    slug: str,
    auction_id: str,
) -> dict[str, Any] | None:
    """
    Find the auction object inside ``pageProps`` for this listing.

    The sold index SSR often returns a page of unrelated auctions; we first match
    ``slug`` / ``auctionId`` on the ``auctions`` array, then fall back to a deep
    search (logged-in SSR sometimes nests the target row).
    """
    if not page_props:
        return None
    auctions = page_props.get("auctions")
    if isinstance(auctions, list):
        for a in auctions:
            if not isinstance(a, dict):
                continue
            vd = a.get("vehicleData") or {}
            if not isinstance(vd, dict):
                continue
            if (vd.get("slug") or "") == slug:
                return a
            if str(a.get("auctionId") or "").upper() == auction_id.upper():
                return a
    found = _deep_find_auction_by_id(page_props, auction_id)
    return found


def parse_summary_from_api_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one element from ``data.auctions``."""
    vd = raw.get("vehicleData") or {}
    if not isinstance(vd, dict):
        vd = {}
    bid_details = raw.get("bidDetails") or {}
    starting = raw.get("startingPrice") or {}
    reserve = raw.get("reservePrice") or {}
    loc = vd.get("location") or {}
    if not isinstance(loc, dict):
        loc = {}

    slug = str(vd.get("slug") or "")
    auction_id = str(raw.get("auctionId") or "")
    year = vd.get("year")
    make = vd.get("make")
    model = vd.get("model")
    title = " ".join(str(x) for x in (year, make, model) if x not in (None, "")).strip()

    bid_value = bid_details.get("value") if isinstance(bid_details, dict) else None
    currency = starting.get("currency") if isinstance(starting, dict) else None
    currency = (currency or "USD") if isinstance(currency, str) else "USD"

    desc = vd.get("description")
    desc = desc if isinstance(desc, str) else ""
    tid = _title_image_description(vd)
    text_for_specs = "\n\n".join(x for x in (desc, tid, model or "") if x).strip()

    fields = extract_description_fields(text_for_specs) if text_for_specs else {}
    mileage = _mileage_from_dupont_model(model) if isinstance(model, str) else None

    return {
        "auction_id": auction_id,
        "slug": slug,
        "title": title,
        "year": (int(year) if isinstance(year, (int, float)) else int(year) if isinstance(year, str) and year.isdigit() else None),
        "make": make,
        "model": model,
        "vehicle_id": str(vd.get("vehicleId") or "") or None,
        "bid_value": int(bid_value) if bid_value is not None else None,
        "currency": currency,
        "status": raw.get("status"),
        "vehicle_status": vd.get("vehicleStatus"),
        "sold_date": raw.get("soldDate") or raw.get("endTime"),
        "lot_number": raw.get("lotNumber"),
        "has_reserve": bool(raw.get("hasReservePrice")),
        "reserve_value": reserve.get("value") if isinstance(reserve, dict) else None,
        "reserve_currency": reserve.get("currency") if isinstance(reserve, dict) else None,
        "image_url": (vd.get("files") or {}).get("titleImage", {}).get("fullUrl")
        if isinstance(vd.get("files"), dict)
        else None,
        "listing_url": f"{BASE_URL}/listings/sold/{slug}" if slug else None,
        "country": location_to_country(loc.get("city"), loc.get("state")),
        "listing_description": desc or None,
        "vin": _extract_vin(text_for_specs) if text_for_specs else None,
        "mileage_mi": int(mileage) if mileage is not None else None,
        "ext_color": fields.get("ext_color"),
        "int_color": fields.get("int_color"),
        "engine_spec": fields.get("engine"),
        "transmission": normalize_transmission(fields.get("transmission")),
        "bid_count": None,
    }


def merge_detail_page_props(summary: dict[str, Any], page_props: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay fields from SSR ``pageProps`` onto ``summary``.

    Supports two page formats:

    * ``/auction/{slug}`` detail page — ``pageProps.auctionData`` is a single
      object whose ``vehicleData`` carries all structured fields (VIN, engine,
      colours, odometer, transmission/driveTrain, trim, titleStatus …).
    * ``/listings/…`` index pages — ``pageProps.auctions`` is an array; we
      search for the matching row via ``pick_enriched_auction`` (legacy path).
    """
    out = dict(summary)
    if not page_props:
        return out

    # --- primary: detail page has auctionData as a single dict ---
    enriched = page_props.get("auctionData")
    if not enriched or not isinstance(enriched, dict):
        # fallback: search an auctions array (index page path)
        enriched = pick_enriched_auction(
            page_props, slug=summary["slug"], auction_id=summary["auction_id"]
        )
    if not enriched:
        return out

    vd = enriched.get("vehicleData") or {}
    if not isinstance(vd, dict):
        return out

    # --- structured fields available directly on the detail page ---
    if vd.get("vin"):
        out["vin"] = str(vd["vin"])
    if vd.get("engine"):
        out["engine_spec"] = str(vd["engine"])
    if vd.get("exteriorColor"):
        out["ext_color"] = str(vd["exteriorColor"])
    if vd.get("interiorColor"):
        out["int_color"] = str(vd["interiorColor"])

    odometer = vd.get("odometer")
    if isinstance(odometer, dict):
        mi = odometer.get("readingMiles")
        if mi is not None:
            try:
                out["mileage_mi"] = int(mi)
            except (TypeError, ValueError):
                pass

    # DuPont often puts gearbox info in ``driveTrain`` (e.g. "7 Speed Dual Clutch AWD").
    raw_tx = None
    tx = vd.get("transmission")
    if isinstance(tx, str) and tx.strip():
        raw_tx = tx.strip()
    elif vd.get("driveTrain"):
        dt = str(vd["driveTrain"]).strip() or None
        # Skip mis-filed engine strings so we can fall back to vehicleContents / SEO text.
        if dt and not _drive_train_looks_like_engine_spec(dt):
            raw_tx = dt
    norm_tx = normalize_transmission(raw_tx)
    if norm_tx:
        out["transmission"] = norm_tx

    # --- description-based fallback extraction (for any fields still missing) ---
    desc = vd.get("description")
    if isinstance(desc, str) and desc.strip():
        out["listing_description"] = desc.strip()
    tid = _title_image_description(vd)
    blob = "\n\n".join(
        x for x in (out.get("listing_description") or "", tid or "", str(vd.get("model") or "")) if x
    )
    if blob.strip():
        if not out.get("vin"):
            out["vin"] = _extract_vin(blob)
        if not out.get("mileage_mi"):
            mm = _mileage_from_dupont_model(str(vd.get("model") or ""))
            if mm is not None:
                out["mileage_mi"] = int(mm)
        if not out.get("engine_spec") or not out.get("ext_color") or not out.get("int_color"):
            fx = extract_description_fields(blob)
            if not out.get("engine_spec") and fx.get("engine"):
                out["engine_spec"] = fx["engine"]
            if not out.get("ext_color") and fx.get("ext_color"):
                out["ext_color"] = fx["ext_color"]
            if not out.get("int_color") and fx.get("int_color"):
                out["int_color"] = fx["int_color"]
            if not out.get("transmission") and fx.get("transmission"):
                out["transmission"] = normalize_transmission(fx["transmission"])

    # DuPont frequently stores the actual write-up in auctionData.vehicleContents HTML blocks.
    # If vehicleData.description is empty, build Listing_description from those blocks.
    if not out.get("listing_description"):
        parts_plain: list[str] = []
        for vc in enriched.get("vehicleContents") or []:
            if not isinstance(vc, dict):
                continue
            c = vc.get("content")
            if isinstance(c, str) and c.strip():
                p = _html_to_plain(c)
                if p:
                    parts_plain.append(p)
        if parts_plain:
            # Keep it bounded to avoid giant payloads in CSV/DB.
            out["listing_description"] = "\n\n".join(parts_plain)[:200_000].strip() or None

    # Long-form copy (overview, seller notes) often mentions gearbox; structured fields may be wrong/empty.
    if not out.get("transmission"):
        parts: list[str] = []
        for vc in enriched.get("vehicleContents") or []:
            if isinstance(vc, dict):
                c = vc.get("content")
                if isinstance(c, str) and c.strip():
                    parts.append(c)
        if parts:
            plain = _html_to_plain(" ".join(parts))
            if plain:
                fx_vc = extract_description_fields(plain[:200_000])
                if fx_vc.get("transmission"):
                    out["transmission"] = fx_vc["transmission"]

    if not out.get("transmission"):
        seo = page_props.get("seo") if isinstance(page_props.get("seo"), dict) else {}
        seo_desc = seo.get("description") if isinstance(seo.get("description"), str) else ""
        if seo_desc.strip():
            fx_seo = extract_description_fields(seo_desc)
            if fx_seo.get("transmission"):
                out["transmission"] = fx_seo["transmission"]

    # Schema: Transmission is ``manual`` / ``automatic`` only (see shared/export.py).
    out["transmission"] = normalize_transmission(out.get("transmission"))

    # --- bid count (detail page exposes totalBidCount) ---
    for key in ("totalBidCount", "bidCount", "numberOfBids"):
        val = enriched.get(key)
        if isinstance(val, int):
            out["bid_count"] = val
            break

    # --- lot number (detail page has lotNumber if listings API doesn't) ---
    if not out.get("lot_number") and enriched.get("lotNumber") is not None:
        out["lot_number"] = enriched["lotNumber"]

    # --- image ---
    img = (vd.get("files") or {}).get("titleImage", {}).get("fullUrl")
    if isinstance(img, str) and img:
        out["image_url"] = img

    # --- location ---
    loc = vd.get("location") or {}
    if isinstance(loc, dict):
        cty = loc.get("city")
        st = loc.get("state")
        if (cty or st) and not out.get("country"):
            out["country"] = location_to_country(
                str(cty) if cty else None,
                str(st) if st else None,
            )
    return out


def sold_status_from_row(summary: dict[str, Any]) -> str:
    """Map DuPont vehicleStatus / status → canonical "Sold" / "Unsold".

    Priority:
      1. vehicleStatus == "sold"  → Sold
      2. vehicleStatus == "unsold" → Unsold
      3. status == "closed" + bid present → Sold (auction ended with bids)
      4. status == "closed" + no bid → Unsold
      5. default → Unsold
    """
    vs = (summary.get("vehicle_status") or "").lower()
    st = (summary.get("status") or "").lower()
    if vs == "sold":
        return "Sold"
    if vs == "unsold":
        return "Unsold"
    if st == "closed":
        return "Sold" if summary.get("bid_value") else "Unsold"
    return "Unsold"
