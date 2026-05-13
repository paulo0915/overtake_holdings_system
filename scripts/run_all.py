"""Full re-scrape orchestrator - runs every source and exports final CSVs.

Intended for a clean delivery run after the DB has been reset with reset_db.py.

Sequence:
  1. [Optional] Reset DB   - pass --reset to truncate both tables first
  2. Auction scrapers      - daily, monthly, then event
  3. Classifieds scrapers  - all sources, used+new where applicable
  4. Export DB to CSV      - dumps auctions_<date>.csv / classifieds_<date>.csv

Usage:
    python scripts/run_all.py                     # run everything (no DB reset)
    python scripts/run_all.py --reset             # wipe DB first, then run all
    python scripts/run_all.py --min-price 0       # override price floor
    python scripts/run_all.py --only bat pcar     # run specific sources only
    python scripts/run_all.py --skip rms bon      # skip specific sources
    python scripts/run_all.py --no-export         # skip final CSV export step

Notes:
  - Vehicle filter is ON by default (--no-filter to disable).
  - Event scrapers run in 'past' mode (full historical archive).
  - Classifieds scrapers with used/new modes run both modes sequentially.
  - Failures in one source are logged but do NOT stop the rest of the run.
  - Estimated total wall-clock time: 2-6 hours depending on network conditions.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source definitions - (source_code, modes, description)
# modes=None means no --mode flag needed.
# ---------------------------------------------------------------------------
_AUCTION_SOURCES = [
    # -- Daily --------------------------------------------------------------
    ("bat",         None,           "Bring a Trailer"),
    ("cab",         None,           "Cars & Bids"),
    ("pcar",        None,           "PCarMarket"),
    ("hag",         None,           "Hagerty Marketplace"),
    ("dpl",         None,           "DuPont Registry Live"),
    ("sbm",         None,           "Sotheby's Motorsport"),
    # -- Monthly ------------------------------------------------------------
    ("mbm",         None,           "The MB Market"),
    ("sbx",         None,           "SBX Cars"),
    ("cbk",         None,           "Carbuckle"),
    ("cc",          None,           "Collecting Cars"),
    ("tmkt",        None,           "The Market (themarket.co.uk)"),
    ("cac",         ["past"],       "Car & Classic"),
    # -- Event --------------------------------------------------------------
    ("barrett",     ["past"],       "Barrett-Jackson"),
    ("mecum",       ["past"],       "Mecum Auctions"),
    ("rms",         ["past"],       "RM Sotheby's"),
    ("bon",         ["past"],       "Bonhams"),
    ("gooding",     None,           "Gooding & Company"),
    ("broadarrow",  ["past"],       "Broad Arrow Auctions"),
    ("worldwide",   None,           "Worldwide Auctioneers"),
]

_CLASSIFIEDS_SOURCES = [
    ("dpr",         None,           "DuPont Registry (classifieds)"),
    ("cars",        None,           "Cars.com"),
    ("je",          None,           "JamesEdition"),
    ("uc",          None,           "UsedCars.com"),
    ("atcom",       ["all"],        "AutoTrader.com"),
    ("cg",          ["used", "new"],"CarGurus"),
    ("tc",          ["used", "new"],"TrueCar"),
    ("atca",        ["used", "new"],"AutoTrader.ca"),
    ("as24",        ["used", "new"],         "AutoScout24"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_scraper(
    source: str,
    mode: str | None,
    *,
    min_price: int | None,
    max_pages: int,
    source_page_overrides: dict[str, int] | None = None,
    extra_args: list[str],
) -> tuple[int, float]:
    """Run run_scraper.py for one source/mode. Returns (exit_code, elapsed_s)."""
    effective_pages = (source_page_overrides or {}).get(source, max_pages)
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_scraper.py"),
        "--source", source,
        "--max-pages", str(effective_pages),
    ]
    if mode:
        cmd += ["--mode", mode]
    if min_price is not None:
        cmd += ["--min-price", str(min_price)]
    cmd += extra_args

    label = f"{source}" + (f"/{mode}" if mode else "")
    print(f"\n{'-' * 60}")
    print(f"  >>  {label}")
    print(f"{'-' * 60}")
    logger.info("Starting: %s", " ".join(cmd))

    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(ROOT))
    elapsed = time.time() - t0

    status = "OK" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    print(f"\n  {status}  -  {elapsed:.0f}s")
    return result.returncode, elapsed


def _reset_db() -> None:
    """Truncate both tables (non-interactive - skip confirm prompt)."""
    from shared.db import get_client
    client = get_client()
    # Delete in small sub-batches to avoid httpx URL-too-long errors when the
    # IN(...) clause contains hundreds of long listing URLs.
    SUB_BATCH = 50
    for table in ("auctions", "classifieds"):
        print(f"  Clearing '{table}' ...", end=" ", flush=True)
        deleted = 0
        while True:
            batch = client.table(table).select("listing_url").limit(200).execute()
            rows = batch.data or []
            if not rows:
                break
            urls = [r["listing_url"] for r in rows if r.get("listing_url")]
            if not urls:
                break
            for i in range(0, len(urls), SUB_BATCH):
                chunk = urls[i:i + SUB_BATCH]
                client.table(table).delete().in_("listing_url", chunk).execute()
            deleted += len(urls)
        print(f"{deleted:,} rows deleted.")


def _export_csv(out_dir: Path) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "export_db_csv.py"),
        "--out", str(out_dir),
    ]
    print(f"\n{'=' * 60}")
    print("  Exporting final CSVs from DB ...")
    print(f"{'=' * 60}")
    subprocess.run(cmd, cwd=str(ROOT))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset", action="store_true",
        help="Truncate both DB tables before scraping (no confirmation prompt)",
    )
    parser.add_argument(
        "--min-price", type=int, default=None,
        help="Override MIN_PRICE_USD for all scrapers (default: use config value)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Max pages per scraper (0=unlimited, default: 0)",
    )
    parser.add_argument(
        "--only", nargs="+", metavar="SOURCE",
        help="Run only these source codes (e.g. --only bat pcar cg)",
    )
    parser.add_argument(
        "--skip", nargs="+", metavar="SOURCE",
        help="Skip these source codes",
    )
    parser.add_argument(
        "--no-export", action="store_true",
        help="Skip the final DB->CSV export step",
    )
    parser.add_argument(
        "--gooding-pages", type=int, default=None,
        help="Override --max-pages for Gooding only (e.g. --gooding-pages 10)",
    )
    parser.add_argument(
        "--sbx-pages", type=int, default=None,
        help="Override --max-pages for SBX Cars (lot count, not pages; e.g. --sbx-pages 30)",
    )
    parser.add_argument(
        "--worldwide-pages", type=int, default=None,
        help="Override --max-pages for Worldwide Auctioneers (lot count; e.g. --worldwide-pages 50)",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Disable vehicle filter for all scrapers (debugging only)",
    )
    parser.add_argument(
        "--output-dir", type=str, default="output",
        help="Output directory for per-scraper CSVs and final export",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(ROOT / "output" / f"run_all_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8"),
        ],
    )
    (ROOT / "output").mkdir(exist_ok=True)

    only = set(args.only) if args.only else None
    skip = set(args.skip) if args.skip else set()
    extra = ["--no-filter"] if args.no_filter else []
    extra += ["--output-dir", args.output_dir]
    source_page_overrides: dict[str, int] = {}
    if args.gooding_pages is not None:
        source_page_overrides["gooding"] = args.gooding_pages
    if args.sbx_pages is not None:
        source_page_overrides["sbx"] = args.sbx_pages
    if args.worldwide_pages is not None:
        source_page_overrides["worldwide"] = args.worldwide_pages

    # -- 1. Optional DB reset ---------------------------------------------
    if args.reset:
        print("\n" + "=" * 60)
        print("  RESETTING DATABASE")
        print("=" * 60)
        _reset_db()

    # -- 2. Build run plan -------------------------------------------------
    all_sources = _AUCTION_SOURCES + _CLASSIFIEDS_SOURCES
    plan: list[tuple[str, str | None, str]] = []
    for code, modes, desc in all_sources:
        if only and code not in only:
            continue
        if code in skip:
            continue
        if modes:
            for m in modes:
                plan.append((code, m, desc))
        else:
            plan.append((code, None, desc))

    print(f"\n{'=' * 60}")
    print(f"  RUN PLAN  -  {len(plan)} scraper run(s)")
    print(f"{'=' * 60}")
    for code, mode, desc in plan:
        label = f"{code}/{mode}" if mode else code
        print(f"  {label:25s}  {desc}")

    print(f"\n  Min price : ${args.min_price:,}" if args.min_price else "\n  Min price : (config default)")
    print(f"  Max pages : {args.max_pages or 'unlimited'}")
    print(f"  Filter    : {'OFF' if args.no_filter else 'ON (default)'}")
    print()

    # -- 3. Execute --------------------------------------------------------
    wall_start = time.time()
    results: list[tuple[str, int, float]] = []

    for code, mode, _desc in plan:
        rc, elapsed = _run_scraper(
            code, mode,
            min_price=args.min_price,
            max_pages=args.max_pages,
            source_page_overrides=source_page_overrides,
            extra_args=extra,
        )
        label = f"{code}/{mode}" if mode else code
        results.append((label, rc, elapsed))

    # -- 4. Export final CSVs ---------------------------------------------
    if not args.no_export:
        _export_csv(Path(args.output_dir))

    # -- 5. Summary --------------------------------------------------------
    wall_elapsed = time.time() - wall_start
    mins, secs = divmod(int(wall_elapsed), 60)

    print(f"\n{'=' * 60}")
    print(f"  RUN COMPLETE  -  {mins}m {secs}s total")
    print(f"{'=' * 60}")
    ok = [(l, e) for l, rc, e in results if rc == 0]
    failed = [(l, rc) for l, rc, e in results if rc != 0]
    print(f"  Succeeded : {len(ok)}")
    if failed:
        print(f"  Failed    : {len(failed)}")
        for label, rc in failed:
            print(f"    FAIL: {label}  (exit {rc})")
    else:
        print("  Failed    : 0  - all sources completed successfully")
    print()


if __name__ == "__main__":
    main()
