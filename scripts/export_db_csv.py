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
from shared.export import export_to_csv, export_to_excel
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
    the end. Returns the full row list (lowercase keys from the DB),
    deduplicated on ``record_id``.

    Why dedup here: ``.range(start, end)`` is offset-based. When many rows
    share the same ``order_by`` value (e.g. a whole auction's lots get
    upserted in one ``insert_auction_records`` call and all share the
    same ``scraped_at``), Postgres' tie-breaker is non-deterministic
    across requests — so the boundary row of one page can re-appear at
    the head of the next. We add ``record_id`` as a secondary sort key
    to make ties deterministic AND drop any straggler dupes in memory
    as a safety net (some PostgREST versions still drift if the page
    boundary lands mid-tie-group).
    """
    client = get_client()
    all_rows: list[dict] = []
    seen_ids: set[str] = set()
    dropped = 0
    start = 0
    while True:
        end = start + PAGE_SIZE - 1  # .range is inclusive on both ends
        batch = (
            client.table(table)
            .select("*")
            .order(order_by, desc=True)
            .order("record_id", desc=True)
            .range(start, end)
            .execute()
        )
        rows = batch.data or []
        if not rows:
            break
        kept = 0
        for r in rows:
            rid = r.get("record_id")
            if rid and rid in seen_ids:
                dropped += 1
                continue
            if rid:
                seen_ids.add(rid)
            all_rows.append(r)
            kept += 1
        logger.info(
            "%s: fetched %s-%s (%s rows this batch, %s kept, %s total)",
            table, start, start + len(rows) - 1, len(rows), kept, len(all_rows),
        )
        if len(rows) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    if dropped:
        logger.info("%s: dropped %s duplicate row(s) across page boundaries",
                    table, dropped)
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


def export_one(table: str, out_path: Path, *, formats: list[str]) -> list[Path]:
    """Fetch + remap + write a single table in each requested format.

    ``formats`` is a list of ``"csv"`` and/or ``"xlsx"``. The base path
    (``out_path``) is treated as the stem; the correct extension is added
    per format. Returns the list of paths written.
    """
    cfg = _TABLES[table]
    logger.info("Fetching %s table ...", table)
    rows = _fetch_all_rows(table, cfg["order_by"])
    logger.info("%s: fetched %s total rows", table, len(rows))

    if not rows:
        logger.warning(
            "%s: no rows — writing empty file(s) with headers only", table,
        )

    records = _remap_keys(rows, cfg["columns"])
    base = out_path.with_suffix("")  # strip whichever ext was passed in
    written: list[Path] = []
    if "csv" in formats:
        p = export_to_csv(records, base.with_suffix(".csv"), cfg["columns"])
        logger.info("%s: wrote %s rows to %s", table, len(records), p.resolve())
        written.append(p)
    if "xlsx" in formats:
        p = export_to_excel(records, base.with_suffix(".xlsx"), cfg["columns"])
        logger.info("%s: wrote %s rows to %s", table, len(records), p.resolve())
        written.append(p)
    return written


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
            "``<table>_<YYYY-MM-DD>.<ext>``. For a single table + single "
            "format you can also pass a specific file path."
        ),
    )
    parser.add_argument(
        "--format", choices=["csv", "xlsx", "both"], default="xlsx",
        help=(
            "Output format(s). Default xlsx (client deliverable). Pass "
            "``both`` to also write a CSV alongside the xlsx, or ``csv`` "
            "for the legacy CSV-only behaviour."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    tables = list(_TABLES) if args.table == "both" else [args.table]
    formats = ["csv", "xlsx"] if args.format == "both" else [args.format]
    out = Path(args.out)
    from datetime import timezone as _tz
    stamp = datetime.now(_tz.utc).strftime("%Y-%m-%d")

    # If ``--out`` points at a real file AND a single table + single format
    # was chosen, honour that exact path. Otherwise treat it as a directory
    # and auto-name each table's file with the correct extension.
    is_explicit_file = (
        out.suffix.lower() in (".csv", ".xlsx")
        and len(tables) == 1
        and len(formats) == 1
    )
    if not is_explicit_file:
        out.mkdir(parents=True, exist_ok=True)

    for table in tables:
        if is_explicit_file:
            target = out
        else:
            # Use the first format's extension as the placeholder; export_one
            # swaps suffix per requested format anyway.
            target = out / f"{table}_{stamp}.{formats[0]}"
        export_one(table, target, formats=formats)


if __name__ == "__main__":
    main()
