"""One-off: set all Worldwide Auctioneers listing_url to the shared results page URL.

Steps:
1. Delete existing worldwide rows from Supabase.
2. Re-insert them from the per-scraper CSV using record_id as the upsert key
   (bypasses the listing_url unique constraint).
3. Patch the final delivery CSV in-place.
"""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from shared.db import get_client
from shared.schemas import AUCTION_COLUMNS

SHARED_URL = "https://listings.worldwideauctioneers.com/results/"
SOURCE = "listings.worldwideauctioneers.com"
PER_SCRAPER_CSV = ROOT / "output" / "worldwide_auctioneers_results.csv"
DELIVERY_CSV = ROOT / "output" / "auctions_2026-05-10.csv"

ALLOWED = {c.lower() for c in AUCTION_COLUMNS}

# ── 1. Load per-scraper CSV ────────────────────────────────────────────────────
with open(PER_SCRAPER_CSV, encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
print(f"Loaded {len(rows)} rows from {PER_SCRAPER_CSV.name}")

# ── 2. Build Supabase records (shared URL, lowercase keys) ─────────────────────
records = []
for row in rows:
    r = {}
    for k, v in row.items():
        key = k.lower()
        if key not in ALLOWED:
            continue
        r[key] = v if v != "" else None
    r["listing_url"] = SHARED_URL
    if r.get("record_id"):
        records.append(r)

print(f"Prepared {len(records)} records for Supabase")

# ── 3. Delete existing worldwide rows ─────────────────────────────────────────
client = get_client()
print("Deleting existing worldwide rows from Supabase...")
client.table("auctions").delete().eq("source", SOURCE).execute()
print("Deleted.")

# ── 4. Re-insert with unique fragment URLs (Supabase internal storage) ─────────
# Supabase's auctions table uses listing_url as the unique constraint, so all 42
# rows must have distinct URLs to be stored. We use #rid fragments internally;
# the delivery CSV (already patched above) shows the shared URL to the client.
# Also: Supabase's record_id column is UUID type — never send our slug-based IDs.
BATCH = 50
inserted = 0
db_records = []
for row in rows:
    r = {}
    for k, v in row.items():
        key = k.lower()
        if key not in ALLOWED or key == "record_id":  # skip — UUID column, auto-generated
            continue
        r[key] = v if v != "" else None
    # Unique fragment URL for DB storage; delivery CSV has the shared URL
    slug_id = row.get("Record_ID") or row.get("record_id") or ""
    r["listing_url"] = f"https://listings.worldwideauctioneers.com/results/#{slug_id}" if slug_id else SHARED_URL
    db_records.append(r)

# Remove within-batch duplicates on listing_url
seen: dict[str, dict] = {}
for r in db_records:
    seen[r["listing_url"]] = r
db_records = list(seen.values())

for i in range(0, len(db_records), BATCH):
    batch = db_records[i : i + BATCH]
    try:
        client.table("auctions").upsert(batch, on_conflict="listing_url").execute()
        inserted += len(batch)
        print(f"  Upserted batch {i // BATCH + 1}: {inserted}/{len(db_records)}")
    except Exception as e:
        print(f"  Batch {i // BATCH + 1} failed: {e}")

print(f"Supabase: inserted {inserted} worldwide rows")

# ── 5. Patch delivery CSV ──────────────────────────────────────────────────────
with open(DELIVERY_CSV, encoding="utf-8", newline="") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    all_rows = list(reader)

patched = 0
for row in all_rows:
    if row.get("Source") == SOURCE:
        row["Listing_url"] = SHARED_URL
        patched += 1

with open(DELIVERY_CSV, encoding="utf-8", newline="") as f:
    pass  # just to confirm readable

with open(DELIVERY_CSV, "w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"Patched {patched} worldwide rows in {DELIVERY_CSV.name}")
print("Done.")
