"""Re-upsert Gooding auction records from local CSV into Supabase.

Run from overtake_holdings_system/:
    python scripts/reupsert_gooding.py
"""

import csv
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

CSV_PATH = PROJECT_ROOT / "output" / "gooding_auction_results.csv"
BATCH_SIZE = 100

_INT_FIELDS = {"Year", "Mileage_mi", "Estimate_low", "Estimate_high", "Selling_price", "Selling_price_USD"}
_FLOAT_FIELDS = {"FX_rate_used"}


def _coerce(key: str, val: str):
    if val == "" or val is None:
        return None
    if key in _INT_FIELDS:
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None
    if key in _FLOAT_FIELDS:
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    return val


def load_csv(path: Path) -> list[dict]:
    records = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rec = {k: _coerce(k, v) for k, v in row.items()}
            records.append(rec)
    log.info("Loaded %d records from %s", len(records), path)
    return records


def main():
    records = load_csv(CSV_PATH)
    if not records:
        log.error("No records found — aborting")
        sys.exit(1)

    from shared.db import insert_auction_records
    try:
        inserted = insert_auction_records(records)
        print(f"Upserted {inserted} Gooding records to Supabase")
    except Exception as e:
        log.error("Supabase upsert failed: %s", e)
        print(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
