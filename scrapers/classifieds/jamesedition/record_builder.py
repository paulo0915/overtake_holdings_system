"""JamesEdition merged rows → ``CLASSIFIEDS_COLUMNS``."""

from __future__ import annotations

from typing import Any


def build_record(
    *,
    flat: dict[str, Any],
    detail: dict[str, Any] | None,
    scraped_at: str,
    today_iso: str,
) -> dict[str, Any]:
    """Assemble the full record for ``shared.db.upsert_classified_record``.

    Merge policy:
        * Year / Make / Model / Title / Image / City / Country / Currency —
          list tile wins (already clean + structured on JE).
        * VIN / Colours / Engine / Gearbox / Mileage / Dealer / Description —
          from the detail page (not present on list tiles).

    Prices:
        ``Asking_price`` is the **source currency** price (JE advertises in
        the seller's native currency). ``Asking_price_USD`` is the
        site-provided pre-converted USD amount (``data-price-usd``) — no FX
        call needed since JE does it for us. ``FX_rate_used`` is computed
        from the pair when both are present.
    """
    detail = detail or {}

    price_src = flat.get("asking_price_src")
    price_usd = flat.get("asking_price_usd_from_list")
    currency = (flat.get("currency") or "USD").upper()

    # FX rate derived from JE's own conversion, to the extent one exists.
    fx_rate = None
    if price_src and price_usd and price_src > 0:
        fx_rate = round(price_usd / price_src, 6)

    return {
        "Record_ID": flat["record_id"],
        "Title": flat.get("title"),
        # Year: list tile (from title) preferred, fall back to detail table.
        "Year": flat.get("year") or detail.get("year"),
        "Make": flat.get("make"),
        "Model": flat.get("model"),
        "VIN": detail.get("vin"),
        "Mileage_mi": detail.get("mileage_mi"),
        "Ext_color": detail.get("ext_color"),
        "Int_color": detail.get("int_color"),
        "Engine_spec": detail.get("engine_spec"),
        "Transmission": detail.get("transmission"),
        "Dealer_name": detail.get("dealer_name"),
        "Asking_price": price_src,
        "Currency": currency,
        "Asking_price_USD": float(price_usd) if price_usd else None,
        "FX_rate_used": fx_rate,
        "Listing_status": "Active",
        "First_seen_date": today_iso,
        "Last_seen_date": today_iso,
        "Last_price_change_date": None,
        "Previous_asking_price": None,
        "Country": flat.get("country") or "USA",
        "Image_url": flat.get("image_url"),
        "Listing_url": flat.get("listing_url"),
        "Source": "jamesedition.com",
        "Listing_segment": "used",
        "Listing_description": detail.get("description"),
        "Scraped_at": scraped_at,
    }
