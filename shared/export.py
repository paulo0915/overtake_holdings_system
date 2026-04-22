"""Export scraped data to CSV and Excel."""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def export_to_csv(records: list[dict], output_path: str | Path, columns: list[str]) -> Path:
    """Export records to CSV with exact column order."""
    df = _build_dataframe(records, columns)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"Exported {len(df)} records to {path}")
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


def _build_dataframe(records: list[dict], columns: list[str]) -> pd.DataFrame:
    """Build DataFrame with exact column ordering and type enforcement."""
    df = pd.DataFrame(records)

    for col in columns:
        if col not in df.columns:
            df[col] = None

    # Derive Auction_status if the record_builder didn't set it.
    #   "Completed" = hammer settled (Selling_price_USD present)
    #   "Upcoming"  = auction not yet concluded (no hammer yet)
    if "Auction_status" in columns:
        def _derive_status(row):
            existing = row.get("Auction_status")
            if existing:
                return existing
            return "Completed" if pd.notna(row.get("Selling_price_USD")) else "Upcoming"
        df["Auction_status"] = df.apply(_derive_status, axis=1)

    df = df[columns]

    int_cols = [c for c in columns if c in (
        "Year", "Mileage_mi", "Selling_price", "Bid_price", "Bid_count",
        "Estimate_low", "Estimate_high", "Asking_price", "Previous_asking_price",
    )]
    for col in int_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    float_cols = [c for c in columns if c in (
        "Selling_price_USD", "Bid_price_USD", "Asking_price_USD", "FX_rate_used",
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
    "Bid_price": "Highest bid in original currency. Unsold only.",
    "Bid_price_USD": "Highest bid converted to USD. Unsold only.",
    "Bid_count": "Number of bids placed.",
    "FX_rate_used": "Exchange rate used for USD conversion. 1.0 if already USD.",
    "Selling_date": "Date of sale/auction end (YYYY-MM-DD).",
    "Auction_house": "Name of auction house or platform.",
    "Country": "Country of listing.",
    "Image_url": "Direct URL to primary listing image.",
    "Listing_url": "Full listing page URL.",
    "Source": "Scrape source domain.",
    "Listing_description": "Full listing description body text.",
    "Scraped_at": "Timestamp in Pacific Time (ISO 8601).",
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
