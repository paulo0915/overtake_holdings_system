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
    """Batch-upsert auction records into Supabase.

    - Enriches each record with ``Auction_status``.
    - Filters each record to only schema columns (drops any scraper-internal fields).
    - Upserts on ``Listing_url`` with ``ignore_duplicates=False`` so re-scraping the
      same URL **overwrites** the prior row. Crucial for the upcoming → past
      lifecycle: a pre-event row (empty hammer, ``Upcoming``) gets replaced by the
      post-event row (hammer price + ``Completed``) when the auction finishes.

    Returns count of records sent to the DB.
    """
    from .export import enrich_records
    from .schemas import AUCTION_COLUMNS

    client = get_client()

    # Populate derived fields + drop records with no URL.
    # Supabase stores column names lowercased (Postgres unquoted identifier rule),
    # while our Python schema is PascalCase — translate on the way out.
    # ``record_id`` is the DB's auto-generated UUID primary key (``gen_random_uuid()``);
    # we must NOT send it so Postgres fills it in. The scraper's Python-side
    # ``Record_ID`` value is dropped at insert time but kept in the CSV export.
    enrich_records(records)
    allowed = set(AUCTION_COLUMNS) - {"Record_ID"}
    valid: list[dict] = []
    dropped_no_url = 0
    for r in records:
        if not r.get("Listing_url"):
            dropped_no_url += 1
            continue
        valid.append({k.lower(): r.get(k) for k in allowed})

    if dropped_no_url:
        logger.warning(
            "Dropped %s record(s) with no Listing_url (cannot upsert without "
            "the conflict key)", dropped_no_url,
        )

    if not valid:
        return 0

    inserted = 0
    for i in range(0, len(valid), AUCTION_BATCH_SIZE):
        batch = valid[i:i + AUCTION_BATCH_SIZE]
        client.table("auctions").upsert(
            batch, on_conflict="listing_url", ignore_duplicates=False,
        ).execute()
        inserted += len(batch)
        logger.debug(f"Batch {i // AUCTION_BATCH_SIZE + 1}: upserted {len(batch)} records")

    logger.info(f"Upserted {inserted} auction records to Supabase")
    return inserted


def update_auction_post_event(lot_number: str, listing_url: str, updates: dict) -> bool:
    """
    Update an auction record after a traditional auction event.

    Matches on ``lot_number`` + ``listing_url`` (Supabase stores unquoted
    identifiers lowercased). Lowercases the ``updates`` dict keys so callers
    can pass PascalCase (matching ``AUCTION_COLUMNS``) or lowercase freely.
    """
    client = get_client()

    lowercased_updates = {k.lower(): v for k, v in (updates or {}).items()}
    result = (
        client.table("auctions")
        .update(lowercased_updates)
        .eq("lot_number", lot_number)
        .eq("listing_url", listing_url)
        .execute()
    )

    if result.data:
        logger.debug(f"Updated post-event: lot={lot_number}")
        return True

    logger.warning(f"No pre-event row found for lot={lot_number}, url={listing_url}")
    return False


# ── Classifieds Database ────────────────────────────────────────

def _lowercase_keys(d: dict) -> dict:
    """Translate PascalCase Python-schema keys to lowercase Postgres columns."""
    return {k.lower(): v for k, v in (d or {}).items()}


def upsert_classified_record(record: dict) -> str:
    """
    Insert or update a classified record with lifecycle logic.

    Dedup key: VIN + Dealer_name + Listing_url (all three).

    Returns: "inserted", "updated", or "skipped"
    """
    from .normalizer import normalize_vin
    from .schemas import CLASSIFIEDS_COLUMNS

    client = get_client()

    # Normalize VIN up-front: rejects placeholders (``.``, ``001``, chassis
    # numbers, ``<vin>ENGINENO<engineno>`` concat bugs) to ``None`` so the
    # column only stores decodable 17-char VINs. See P1 deep-dive §1.
    record["VIN"] = normalize_vin(record.get("VIN"))

    vin = record.get("VIN")
    dealer = record.get("Dealer_name")
    url = record.get("Listing_url")

    if not url:
        return "skipped"

    query = client.table("classifieds").select("*").eq("listing_url", url)
    if vin:
        query = query.eq("vin", vin)
    if dealer:
        query = query.eq("dealer_name", dealer)

    existing = query.execute()

    if not existing.data:
        record["Listing_status"] = "Active"
        # Strip ``Record_ID`` (Postgres UUID default will fill it in) and any
        # fields outside the canonical schema — matches the auctions-insert policy.
        allowed = set(CLASSIFIEDS_COLUMNS) - {"Record_ID"}
        filtered = {k.lower(): record.get(k) for k in allowed}
        # Legacy Supabase deployments have ``classifieds.record_id`` as ``TEXT
        # NOT NULL`` without a default. Supply a UUID so the insert works on
        # both the old and new schema.
        import uuid

        filtered["record_id"] = str(uuid.uuid4())
        client.table("classifieds").insert(filtered).execute()
        return "inserted"

    row = existing.data[0]

    # Price change detection — use `is not None` to handle zero prices correctly
    old_price = row.get("asking_price")
    new_price = record.get("Asking_price")

    row_pk = row.get("record_id")

    if old_price is not None and new_price is not None and old_price != new_price:
        updates = {
            "previous_asking_price": old_price,
            "asking_price": new_price,
            "asking_price_usd": record.get("Asking_price_USD"),
            "fx_rate_used": record.get("FX_rate_used"),
            "last_price_change_date": record.get("Last_seen_date"),
            "last_seen_date": record.get("Last_seen_date"),
            "scraped_at": record.get("Scraped_at"),
        }
        client.table("classifieds").update(updates).eq("record_id", row_pk).execute()
        return "updated"

    # Same price — just update Last_seen_date
    client.table("classifieds").update({
        "last_seen_date": record.get("Last_seen_date"),
        "scraped_at": record.get("Scraped_at"),
    }).eq("record_id", row_pk).execute()
    return "skipped"


def mark_removed_classifieds(source: str, active_urls: set[str]) -> int:
    """
    Batch-mark classifieds as 'Removed' if Active but not in current scrape.

    Fetches all active IDs for the source, filters locally, then issues a
    single batch update for removed ones.
    """
    client = get_client()

    active_rows = (
        client.table("classifieds")
        .select("record_id, listing_url")
        .eq("source", source)
        .eq("listing_status", "Active")
        .execute()
    )

    to_remove = [
        row["record_id"] for row in active_rows.data
        if row["listing_url"] not in active_urls
    ]

    if not to_remove:
        return 0

    client.table("classifieds").update({
        "listing_status": "Removed",
    }).in_("record_id", to_remove).execute()

    logger.info(f"Marked {len(to_remove)} classifieds as Removed for {source}")
    return len(to_remove)


def check_relisted(vin: str, dealer: str, new_url: str) -> bool:
    """Check if a listing is a relist (same VIN + dealer, different URL)."""
    client = get_client()

    existing = (
        client.table("classifieds")
        .select("record_id")
        .eq("vin", vin)
        .eq("dealer_name", dealer)
        .neq("listing_url", new_url)
        .execute()
    )

    return bool(existing.data)
