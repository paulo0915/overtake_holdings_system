"""CarGurus merged rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
    source: str = "cargurus.com",
    listing_segment: str | None = None,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Merge policy:
        * Colors / engine / transmission / VIN / price / mileage / image —
          prefer detail (richer source), fall back to list.
        * Dealer name — list first (detail's ``data.seller`` isn't always
          populated on the first loader response), fall back to detail.
        * Title — synthesize from ``ontologyData`` parts; falls back to the
          raw ``listingTitle`` which carries trim but sometimes ad-copy
          suffixes like ``- $299,900``.
    """
    detail = detail or {}

    def pick(key: str, *, prefer_detail: bool = True):
        """Pick a field, preferring detail unless caller says otherwise."""
        d, f = detail.get(key), flat.get(key)
        if prefer_detail:
            return d if (d is not None and d != "") else f
        return f if (f is not None and f != "") else d

    title = _compose_title(flat) or flat.get("listing_title")

    return {
        "Record_ID": flat["record_id"],  # dropped by db.py insert, kept for CSV
        "Title": title,
        "Year": flat.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": pick("vin"),
        "Mileage_mi": pick("mileage_mi") if pick("mileage_mi") is not None else 0,
        "Ext_color": pick("ext_color"),
        "Int_color": pick("int_color"),
        "Engine_spec": pick("engine_spec"),
        "Transmission": pick("transmission"),
        "Dealer_name": pick("dealer_name", prefer_detail=False),
        "Asking_price": pick("asking_price"),
        "Currency": flat.get("currency") or "USD",
        # CarGurus is USD-native.
        "Asking_price_USD": float(pick("asking_price")) if pick("asking_price") is not None else None,
        "FX_rate_used": 1.0 if pick("asking_price") is not None else None,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "USA",
        "Image_url": pick("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": source,
        "Listing_segment": listing_segment,
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }


def _compose_title(flat: dict[str, Any]) -> str | None:
    """``<year> <make> <model> <trim>`` from the tile's ontologyData."""
    parts = [str(p) for p in (
        flat.get("year"), flat.get("make"), flat.get("model"), flat.get("trim"),
    ) if p]
    return " ".join(parts).strip() or None
