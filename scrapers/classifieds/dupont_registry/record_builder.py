"""DuPont REGISTRY merged rows -> ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _ms_to_date(ms: int | None) -> str | None:
    """Epoch-ms → ``YYYY-MM-DD`` UTC."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError):
        return None


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
) -> dict[str, Any]:
    """Assemble the full record dict for ``shared.db.upsert_classified_record``.

    Keys stay PascalCase because that helper lowercases on insert and
    references ``VIN`` / ``Dealer_name`` / ``Listing_url`` by the PascalCase
    names its internal query logic expects.
    """
    detail = detail or {}
    first_seen = _ms_to_date(flat.get("created_at_ms")) or today_iso

    # Prefer detail-page colours when the list-page gave us "Other" (a common
    # dealer placeholder).
    ext = flat.get("ext_color")
    if (not ext or ext.lower() == "other") and detail.get("ext_color"):
        ext = detail["ext_color"]
    intc = flat.get("int_color")
    if (not intc or intc.lower() == "other") and detail.get("int_color"):
        intc = detail["int_color"]

    return {
        "Record_ID": flat["record_id"],  # dropped by db.py insert, kept for CSV
        "Title": flat.get("title"),
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": flat.get("vin"),
        "Mileage_mi": flat.get("mileage_mi"),
        "Ext_color": ext,
        "Int_color": intc,
        "Engine_spec": detail.get("engine_spec"),
        "Transmission": detail.get("transmission"),
        "Dealer_name": flat.get("dealer_name"),
        "Asking_price": flat.get("asking_price"),
        "Currency": flat.get("currency") or "USD",
        # DuPont is USD-native so Asking_price_USD == Asking_price and FX = 1.
        "Asking_price_USD": float(flat["asking_price"]) if flat.get("asking_price") is not None else None,
        "FX_rate_used": 1.0 if flat.get("asking_price") is not None else None,
        "Listing_status": "Active",
        "First_seen_date": first_seen,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country"),
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": "dupontregistry.com",
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }
