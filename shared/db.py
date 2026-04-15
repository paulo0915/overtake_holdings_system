"""Supabase database client — insert, upsert, dedup logic."""

import logging
from typing import Any

from supabase import create_client, Client

from .config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    """Get or create Supabase client singleton."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY must be set in .env"
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized")
    return _client


# ── Auction Database ────────────────────────────────────────────

def insert_auction_records(records: list[dict]) -> int:
    """
    Insert auction records with dedup by Listing_url.

    Returns count of newly inserted records.
    """
    client = get_client()
    inserted = 0

    for record in records:
        url = record.get("Listing_url")
        if not url:
            continue

        # Check if already exists
        existing = (
            client.table("auctions")
            .select("Record_ID")
            .eq("Listing_url", url)
            .execute()
        )

        if existing.data:
            logger.debug(f"Skipping duplicate auction: {url}")
            continue

        client.table("auctions").insert(record).execute()
        inserted += 1

    logger.info(f"Inserted {inserted}/{len(records)} auction records ({len(records) - inserted} duplicates skipped)")
    return inserted


def update_auction_post_event(lot_number: str, listing_url: str, updates: dict) -> bool:
    """
    Update an auction record after a traditional auction event.

    Matches on Lot_number + Listing_url (pre-event row), updates
    Selling_price, Sold_status, etc. in place.
    """
    client = get_client()

    result = (
        client.table("auctions")
        .update(updates)
        .eq("Lot_number", lot_number)
        .eq("Listing_url", listing_url)
        .execute()
    )

    if result.data:
        logger.debug(f"Updated post-event: lot={lot_number}")
        return True

    logger.warning(f"No pre-event row found for lot={lot_number}, url={listing_url}")
    return False


# ── Classifieds Database ────────────────────────────────────────

def upsert_classified_record(record: dict) -> str:
    """
    Insert or update a classified record with lifecycle logic.

    Dedup key: VIN + Dealer_name + Listing_url (all three).

    Returns: "inserted", "updated", or "skipped"
    """
    client = get_client()

    vin = record.get("VIN")
    dealer = record.get("Dealer_name")
    url = record.get("Listing_url")

    if not url:
        return "skipped"

    # Build query — match on all three fields
    query = client.table("classifieds").select("*").eq("Listing_url", url)
    if vin:
        query = query.eq("VIN", vin)
    if dealer:
        query = query.eq("Dealer_name", dealer)

    existing = query.execute()

    if not existing.data:
        # New listing
        record["Listing_status"] = "Active"
        client.table("classifieds").insert(record).execute()
        return "inserted"

    row = existing.data[0]

    # Price change detection
    old_price = row.get("Asking_price")
    new_price = record.get("Asking_price")

    if old_price and new_price and old_price != new_price:
        updates = {
            "Previous_asking_price": old_price,
            "Asking_price": new_price,
            "Asking_price_USD": record.get("Asking_price_USD"),
            "FX_rate_used": record.get("FX_rate_used"),
            "Last_price_change_date": record.get("Last_seen_date"),
            "Last_seen_date": record.get("Last_seen_date"),
            "Scraped_at": record.get("Scraped_at"),
        }
        client.table("classifieds").update(updates).eq("Record_ID", row["Record_ID"]).execute()
        return "updated"

    # Same price — just update Last_seen_date
    client.table("classifieds").update({
        "Last_seen_date": record.get("Last_seen_date"),
        "Scraped_at": record.get("Scraped_at"),
    }).eq("Record_ID", row["Record_ID"]).execute()
    return "skipped"


def mark_removed_classifieds(source: str, active_urls: set[str]) -> int:
    """
    Mark classifieds as 'Removed' if they were Active but not in current scrape.

    Returns count of removed listings.
    """
    client = get_client()

    active_rows = (
        client.table("classifieds")
        .select("Record_ID, Listing_url")
        .eq("Source", source)
        .eq("Listing_status", "Active")
        .execute()
    )

    removed = 0
    for row in active_rows.data:
        if row["Listing_url"] not in active_urls:
            client.table("classifieds").update({
                "Listing_status": "Removed",
            }).eq("Record_ID", row["Record_ID"]).execute()
            removed += 1

    if removed:
        logger.info(f"Marked {removed} classifieds as Removed for {source}")
    return removed


def check_relisted(vin: str, dealer: str, new_url: str) -> bool:
    """Check if a listing is a relist (same VIN + dealer, different URL)."""
    client = get_client()

    existing = (
        client.table("classifieds")
        .select("Record_ID")
        .eq("VIN", vin)
        .eq("Dealer_name", dealer)
        .neq("Listing_url", new_url)
        .execute()
    )

    return bool(existing.data)
