"""Dump the entire ``auctions`` table from Supabase to a single CSV.

Usage:
    python scripts/export_auctions_csv.py                           # → output/auctions_<date>.csv
    python scripts/export_auctions_csv.py --out /path/to/file.csv   # custom path

Pages the table in chunks of ``PAGE_SIZE`` (PostgREST's default limit is
1000 rows) and streams rows into a pandas DataFrame keyed by the exact
column order in ``shared.schemas.AUCTION_COLUMNS``.

Postgres stores unquoted identifiers lowercased, so the DB has
``record_id``/``title``/``year`` while our schema uses PascalCase
(``Record_ID``/``Title``/``Year``). We remap on the way out.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.db import get_client
from shared.export import export_to_csv
from shared.schemas import AUCTION_COLUMNS

logger = logging.getLogger(__name__)

# PostgREST's default / max rows per request is 1000. Stay at 1000 for
# minimum round-trips; Supabase supports ``.range(start, end)`` pagination.
PAGE_SIZE = 1000


def _lower_to_pascal_map() -> dict[str, str]:
    """``{"record_id": "Record_ID", "title": "Title", ...}``.

    Built from ``AUCTION_COLUMNS`` so the source of truth is the schema,
    not an ad-hoc list. If a new column is added to the schema it'll
    remap automatically.
    """
    return {col.lower(): col for col in AUCTION_COLUMNS}


def _fetch_all_rows() -> list[dict]:
    """Page through ``auctions`` until PostgREST returns less than a full
    page. Returns the full row list (all lowercase keys from the DB).
    """
    client = get_client()
    all_rows: list[dict] = []
    start = 0
    while True:
        end = start + PAGE_SIZE - 1  # .range is inclusive on both ends
        batch = (
            client.table("auctions")
            .select("*")
            .order("scraped_at", desc=True)
            .range(start, end)
            .execute()
        )
        rows = batch.data or []
        if not rows:
            break
        all_rows.extend(rows)
        logger.info("Fetched %s–%s (%s rows this batch, %s total)",
                    start, start + len(rows) - 1, len(rows), len(all_rows))
        if len(rows) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return all_rows


def _remap_keys(rows: list[dict]) -> list[dict]:
    """Rename lowercase DB keys → PascalCase schema keys so the CSV headers
    match the canonical column names the rest of the project uses.
    """
    lookup = _lower_to_pascal_map()
    remapped: list[dict] = []
    for row in rows:
        out: dict = {}
        for k, v in row.items():
            out[lookup.get(k, k)] = v
        remapped.append(out)
    return remapped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_out = Path("output") / f"auctions_{datetime.utcnow():%Y-%m-%d}.csv"
    parser.add_argument(
        "--out", type=str, default=str(default_out),
        help=f"Destination CSV path (default: {default_out})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    logger.info("Fetching auctions table …")
    rows = _fetch_all_rows()
    logger.info("Fetched %s total rows", len(rows))

    if not rows:
        logger.warning("No rows returned — writing empty CSV with headers only")

    records = _remap_keys(rows)
    path = export_to_csv(records, args.out, AUCTION_COLUMNS)
    logger.info("✓ Wrote %s rows to %s", len(records), path.resolve())


if __name__ == "__main__":
    main()
