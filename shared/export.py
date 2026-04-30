"""Export scraped data to CSV and Excel."""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def export_to_csv(records: list[dict], output_path: str | Path, columns: list[str]) -> Path:
    """Export records to CSV with exact column order.

    Also writes a ``data_dictionary.csv`` alongside the main CSV (same
    directory, stem + ``_data_dictionary``), satisfying Deliverable #7 of the
    brief which requires a Data Dictionary for Claude/AI compatibility.

    Encoding is plain ``utf-8`` (no BOM). ``utf-8-sig`` adds an ``EF BB BF``
    prefix that Excel-for-Windows likes, but many consumers — Python's
    stdlib ``csv.DictReader`` without ``utf-8-sig``, ``awk``/``grep``
    pipelines, Node's ``csv-parse``, Go/Rust default CSV libs — treat the
    BOM as part of the first column header and break. Modern Excel / pandas
    handle bare UTF-8 fine.
    """
    df = _build_dataframe(records, columns)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    logger.info(f"Exported {len(df)} records to {path}")

    # Write accompanying data_dictionary.csv (Deliverable #7)
    dict_path = path.parent / (path.stem + "_data_dictionary.csv")
    _write_data_dictionary_csv(dict_path, columns)
    logger.info(f"Exported data dictionary to {dict_path}")

    return path


def export_to_excel(records: list[dict], output_path: str | Path, columns: list[str]) -> Path:
    """Export records to Excel with Data Dictionary sheet."""
    df = _build_dataframe(records, columns)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Results", index=False)
        _write_data_dictionary(writer, columns)

    logger.info(f"Exported {len(df)} records to {path}")
    return path


def enrich_records(records: list[dict]) -> list[dict]:
    """Populate derived ``Auction_status`` and coerce ``Transmission`` in place.

    Idempotent — if a record_builder already set the field, it is preserved.
    Called by ``export_to_csv``/``export_to_excel`` and by the DB-insert path,
    so every record hits Supabase with this column populated.

    ``Transmission`` is the canonical ``"Manual"`` / ``"Automatic"`` enum; anything
    else (including the legacy lowercase values that used to come out of
    ``normalize_transmission``) is re-normalized here as a safety net.
    """
    from .normalizer import (
        canonical_country_name,
        normalize_engine_spec,
        normalize_make_model,
        normalize_transmission,
        normalize_vin,
        parse_year_from_title,
    )

    for r in records:
        if not r.get("Auction_status"):
            r["Auction_status"] = "Completed" if r.get("Selling_price_USD") is not None else "Upcoming"
        tx = r.get("Transmission")
        if tx not in (None, "Manual", "Automatic"):
            r["Transmission"] = normalize_transmission(tx)
        if r.get("Country"):
            r["Country"] = canonical_country_name(r["Country"])
        if r.get("Engine_spec"):
            r["Engine_spec"] = normalize_engine_spec(r["Engine_spec"])
        if "VIN" in r:
            r["VIN"] = normalize_vin(r.get("VIN"))

        # Fix multi-word make splits (Aston Martin, Land Rover, Alfa Romeo…),
        # canonical casing (McLaren, GMC), and pseudo-make remaps (Range Rover).
        # Also nulls "Unspecified" / placeholder make/model strings.
        make, model = normalize_make_model(
            r.get("Make"), r.get("Model"), title=r.get("Title")
        )
        r["Make"] = make
        r["Model"] = model

        # Year: if the stored Year disagrees with the 4-digit year at the start
        # of the Title, the Title is treated as authoritative (brief §5 / client
        # Issues Report Section 5).  Only override when the title contains a
        # leading year and the current Year value is wrong.
        title = r.get("Title") or ""
        if title:
            title_year = parse_year_from_title(title)
            if title_year and r.get("Year") != title_year:
                r["Year"] = title_year

    return records


def _build_dataframe(records: list[dict], columns: list[str]) -> pd.DataFrame:
    """Build DataFrame with exact column ordering and type enforcement."""
    enrich_records(records)
    df = pd.DataFrame(records)

    for col in columns:
        if col not in df.columns:
            df[col] = None

    df = df[columns]

    int_cols = [c for c in columns if c in (
        "Year", "Mileage_mi", "Selling_price",
        "Estimate_low", "Estimate_high", "Asking_price", "Previous_asking_price",
    )]
    for col in int_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    float_cols = [c for c in columns if c in (
        "Selling_price_USD", "Asking_price_USD", "FX_rate_used",
    )]
    for col in float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# Column descriptions for Data Dictionary
_COLUMN_DESCRIPTIONS = {
    "Record_ID": "Stable listing ID from source. Consistent across scrape runs.",
    "Title": "Full listing title as it appears on the source.",
    "Year": "Model year (4-digit YYYY).",
    "Make": "Manufacturer name from source structured data.",
    "Model": "Full model name + variant.",
    "VIN": "VIN or chassis number. 17-char VIN preferred.",
    "Mileage_mi": "Odometer in miles. KM converted. Priority: title > sidebar > description.",
    "Ext_color": "Exterior colour from sidebar or description.",
    "Int_color": "Interior colour/upholstery. Full string.",
    "Engine_spec": "Engine/drivetrain spec. Normalized to X.X-Liter format.",
    "Transmission": "'manual' or 'automatic' only.",
    "Lot_number": "Auction lot number.",
    "Estimate_low": "Low pre-sale estimate (traditional auctions).",
    "Estimate_high": "High pre-sale estimate (traditional auctions).",
    "Estimate_currency": "Currency of estimate.",
    "Sold_status": "'Sold', 'Unsold', or 'Withdrawn'.",
    "Auction_status": "'Completed' (hammer settled) or 'Upcoming' (not yet concluded).",
    "Currency": "Original listing currency.",
    "Selling_price": "Sale price in original currency. Sold only.",
    "Selling_price_USD": "Sale price converted to USD. Sold only.",
    "FX_rate_used": "Exchange rate used for USD conversion. 1.0 if already USD.",
    "Selling_date": "Date of sale/auction end (YYYY-MM-DD).",
    "Auction_house": "Name of auction house or platform.",
    "Country": "Country of listing.",
    "Image_url": "Direct URL to primary listing image.",
    "Listing_url": "Full listing page URL.",
    "Source": "Scrape source domain.",
    "Listing_description": "Full listing description body text.",
    "Listing_segment": "Inventory segment tag: 'used', 'new', or 'all'. Separate from Source domain.",
    "Scraped_at": "Timestamp when the record was scraped. ISO 8601 UTC, e.g. 2026-04-22T23:29:05Z.",
    # Classifieds-specific
    "Dealer_name": "Selling dealer or seller name.",
    "Asking_price": "Listed asking price in original currency.",
    "Asking_price_USD": "Asking price converted to USD.",
    "Listing_status": "'Active', 'Removed', or 'Relisted'.",
    "First_seen_date": "Date listing first scraped (YYYY-MM-DD).",
    "Last_seen_date": "Last date listing was live (YYYY-MM-DD).",
    "Last_price_change_date": "Date of last price change (YYYY-MM-DD).",
    "Previous_asking_price": "Price before last change.",
}


def _write_data_dictionary(writer: pd.ExcelWriter, columns: list[str]) -> None:
    """Write Data Dictionary sheet for the given columns."""
    rows = []
    for col in columns:
        rows.append({
            "Column": col,
            "Description": _COLUMN_DESCRIPTIONS.get(col, ""),
        })
    pd.DataFrame(rows).to_excel(writer, sheet_name="Data Dictionary", index=False)


def _write_data_dictionary_csv(path: Path, columns: list[str]) -> None:
    """Write a standalone data_dictionary.csv alongside a CSV export."""
    rows = [
        {"Column": col, "Description": _COLUMN_DESCRIPTIONS.get(col, "")}
        for col in columns
    ]
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
