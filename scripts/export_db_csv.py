"""Dump Supabase tables to CSV — ``auctions`` and/or ``classifieds``.

Usage:
    python scripts/export_db_csv.py                     # both tables → output/<table>_<date>.csv
    python scripts/export_db_csv.py --table auctions    # just one
    python scripts/export_db_csv.py --out /custom/dir/  # custom output dir

Pages each table in 1000-row chunks (PostgREST's default limit) and
writes via ``shared.export.export_to_csv`` so the column order matches
the canonical schema (``AUCTION_COLUMNS`` / ``CLASSIFIEDS_COLUMNS``) and
every row passes through the same ``enrich_records`` pipeline that
production scrapes use (VIN normalization, country canonicalisation,
engine-spec cleanup, etc.).

Postgres stores unquoted identifiers lowercased, so the DB has
``record_id``/``title`` while our schema uses PascalCase. We remap on
the way out.
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
from shared.schemas import AUCTION_COLUMNS, CLASSIFIEDS_COLUMNS

logger = logging.getLogger(__name__)

# PostgREST's default / max rows per request. Supabase supports
# ``.range(start, end)`` pagination up to this size.
PAGE_SIZE = 1000

# Registry of exportable tables. Adding a new table is two lines here +
# a schema entry in ``shared/schemas.py``.
_TABLES = {
    "auctions": {
        "columns": AUCTION_COLUMNS,
        "order_by": "scraped_at",
    },
    "classifieds": {
        "columns": CLASSIFIEDS_COLUMNS,
        "order_by": "scraped_at",
    },
}


def _lower_to_pascal_map(columns: list[str]) -> dict[str, str]:
    """``{"record_id": "Record_ID", ...}`` — built from the schema so new
    columns automatically remap.
    """
    return {col.lower(): col for col in columns}


def _fetch_all_rows(table: str, order_by: str) -> list[dict]:
    """Page through the table until a short batch signals we've reached
    the end. Returns the full row list (lowercase keys from the DB).
    """
    client = get_client()
    all_rows: list[dict] = []
    start = 0
    while True:
        end = start + PAGE_SIZE - 1  # .range is inclusive on both ends
        batch = (
            client.table(table)
            .select("*")
            .order(order_by, desc=True)
            .range(start, end)
            .execute()
        )
        rows = batch.data or []
        if not rows:
            break
        all_rows.extend(rows)
        logger.info(
            "%s: fetched %s-%s (%s rows this batch, %s total)",
            table, start, start + len(rows) - 1, len(rows), len(all_rows),
        )
        if len(rows) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return all_rows


def _remap_keys(rows: list[dict], columns: list[str]) -> list[dict]:
    """Rename lowercase DB keys → PascalCase schema keys so the CSV
    headers match the canonical column names the rest of the project
    uses.
    """
    lookup = _lower_to_pascal_map(columns)
    remapped: list[dict] = []
    for row in rows:
        out: dict = {}
        for k, v in row.items():
            out[lookup.get(k, k)] = v
        remapped.append(out)
    return remapped


def export_one(table: str, out_path: Path) -> Path:
    """Fetch + remap + write a single table. Returns the resolved path."""
    cfg = _TABLES[table]
    logger.info("Fetching %s table ...", table)
    rows = _fetch_all_rows(table, cfg["order_by"])
    logger.info("%s: fetched %s total rows", table, len(rows))

    if not rows:
        logger.warning(
            "%s: no rows — writing empty CSV with headers only", table,
        )

    records = _remap_keys(rows, cfg["columns"])
    path = export_to_csv(records, out_path, cfg["columns"])
    logger.info("%s: wrote %s rows to %s", table, len(records), path.resolve())
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--table", choices=list(_TABLES) + ["both"], default="both",
        help="Which table to dump (default: both)",
    )
    parser.add_argument(
        "--out", type=str, default="output",
        help=(
            "Output directory (default: output/). Filenames are "
            "``<table>_<YYYY-MM-DD>.csv``. For a single table you can "
            "also pass a specific file path."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    tables = list(_TABLES) if args.table == "both" else [args.table]
    out = Path(args.out)
    from datetime import timezone as _tz
    stamp = datetime.now(_tz.utc).strftime("%Y-%m-%d")

    # If ``--out`` points at a .csv file AND a single table was chosen,
    # honour that exact path. Otherwise treat it as a directory and
    # auto-name each table's file.
    is_explicit_file = out.suffix.lower() == ".csv" and len(tables) == 1
    if not is_explicit_file:
        out.mkdir(parents=True, exist_ok=True)

    for table in tables:
        target = out if is_explicit_file else out / f"{table}_{stamp}.csv"
        export_one(table, target)


if __name__ == "__main__":
    main()
