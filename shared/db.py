"""Supabase database client — batch insert, upsert, dedup logic."""

import logging

from supabase import create_client, Client

from .config import SUPABASE_URL, SUPABASE_KEY

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    """Get or create Supabase client singleton."""
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized")
    return _client


# ── Auction Database ────────────────────────────────────────────

AUCTION_BATCH_SIZE = 100


def insert_auction_records(records: list[dict]) -> int:
    """
    Batch-insert auction records with dedup by Listing_url.

    Uses upsert with on_conflict=Listing_url so the DB handles dedup
    in a single call per batch instead of N+1 queries.

    Returns count of records sent (DB silently skips duplicates).
    """
    client = get_client()

    # Filter out records with no URL
    valid = [r for r in records if r.get("Listing_url")]
    if not valid:
        return 0

    inserted = 0
    for i in range(0, len(valid), AUCTION_BATCH_SIZE):
        batch = valid[i:i + AUCTION_BATCH_SIZE]
        client.table("auctions").upsert(
            batch, on_conflict="Listing_url", ignore_duplicates=True,
        ).execute()
        inserted += len(batch)
        logger.debug(f"Batch {i // AUCTION_BATCH_SIZE + 1}: sent {len(batch)} records")

    logger.info(f"Sent {inserted} auction records to Supabase (duplicates ignored by DB)")
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

    query = client.table("classifieds").select("*").eq("Listing_url", url)
    if vin:
        query = query.eq("VIN", vin)
    if dealer:
        query = query.eq("Dealer_name", dealer)

    existing = query.execute()

    if not existing.data:
        record["Listing_status"] = "Active"
        client.table("classifieds").insert(record).execute()
        return "inserted"

    row = existing.data[0]

    # Price change detection — use `is not None` to handle zero prices correctly
    old_price = row.get("Asking_price")
    new_price = record.get("Asking_price")

    if old_price is not None and new_price is not None and old_price != new_price:
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
    Batch-mark classifieds as 'Removed' if Active but not in current scrape.

    Fetches all active Record_IDs for the source, filters locally,
    then issues a single batch update for removed ones.
    """
    client = get_client()

    active_rows = (
        client.table("classifieds")
        .select("Record_ID, Listing_url")
        .eq("Source", source)
        .eq("Listing_status", "Active")
        .execute()
    )

    to_remove = [
        row["Record_ID"] for row in active_rows.data
        if row["Listing_url"] not in active_urls
    ]

    if not to_remove:
        return 0

    # Batch update using IN filter
    client.table("classifieds").update({
        "Listing_status": "Removed",
    }).in_("Record_ID", to_remove).execute()

    logger.info(f"Marked {len(to_remove)} classifieds as Removed for {source}")
    return len(to_remove)


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
