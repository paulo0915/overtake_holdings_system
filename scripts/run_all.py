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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)

# Sane default for parallel scraping. The bottleneck is the network and the
# remote sites' rate limits (each scraper has its own per-host concurrency
# cap), not local CPU. 6 workers gives ~5x speedup vs sequential without
# triggering shared-IP rate limits across sources.
_DEFAULT_PARALLEL_WORKERS = 6

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
    ("barrett",     ["past", "upcoming"], "Barrett-Jackson"),
    ("mecum",       ["past", "upcoming"], "Mecum Auctions"),
    # Sources with both past+upcoming modes are listed with both so the
    # --auction-mode flag below can pick which to run. Sources that don't
    # yet have upcoming-mode code stay past-only.
    ("rms",         ["past", "upcoming"], "RM Sotheby's"),
    ("bon",         ["past", "upcoming"], "Bonhams"),
    ("gooding",     ["past", "upcoming"], "Gooding & Company"),
    ("broadarrow",  ["past", "upcoming"], "Broad Arrow Auctions"),
    ("worldwide",   None,                 "Worldwide Auctioneers"),
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

_print_lock = threading.Lock()


def _build_cmd(
    source: str,
    mode: str | None,
    *,
    min_price: int | None,
    max_pages: int,
    source_page_overrides: dict[str, int] | None,
    extra_args: list[str],
) -> list[str]:
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
    return cmd


def _run_one(
    source: str,
    mode: str | None,
    cmd: list[str],
    *,
    log_dir: Path,
    sequential: bool,
    env: dict[str, str] | None = None,
) -> tuple[str, int, float, Path]:
    """Run one scraper subprocess. Returns (label, rc, elapsed, log_path).

    In sequential mode, output streams to the parent terminal so a single
    scrape feels interactive. In parallel mode, output is captured to a
    per-source log file and only a one-line summary is printed on completion
    — interleaving stdout from 6 subprocesses would be unreadable.
    """
    label = f"{source}" + (f"/{mode}" if mode else "")
    log_path = log_dir / f"{label.replace('/', '-')}.log"
    t0 = time.time()

    if sequential:
        with _print_lock:
            print(f"\n{'-' * 60}\n  >>  {label}\n{'-' * 60}")
        result = subprocess.run(cmd, cwd=str(ROOT), env=env)
        rc = result.returncode
        log_path.write_text(
            f"(sequential mode — output streamed to terminal)\n",
            encoding="utf-8",
        )
    else:
        with _print_lock:
            print(f"  [start]  {label}")
        # No subprocess timeout: full archive scrapes (Mecum: 220k lots,
        # BaT: 10k detail fetches, Gooding: 9k lots) legitimately take
        # hours. Hung scrapers are caught by their own internal HTTP
        # timeouts + retry policies, not a wall-clock cap here.
        result = subprocess.run(
            cmd, cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace",
            env=env,
        )
        rc = result.returncode
        log_path.write_text(
            f"$ {' '.join(cmd)}\n\n"
            f"--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n",
            encoding="utf-8",
        )

    elapsed = time.time() - t0
    status = "OK" if rc == 0 else f"FAIL(exit {rc})"
    with _print_lock:
        print(f"  [{status:>8}]  {label:<22}  {elapsed:>5.0f}s   log: {log_path.name}")
    return label, rc, elapsed, log_path


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


def _export_final(out_dir: Path, fmt: str = "xlsx") -> None:
    """Run the DB-dump script in the requested output format(s).

    Default is ``xlsx`` (client deliverable). Pass ``"both"`` to also
    write a CSV alongside, or ``"csv"`` for the legacy behaviour.
    """
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "export_db_csv.py"),
        "--out", str(out_dir),
        "--format", fmt,
    ]
    label = "xlsx" if fmt == "xlsx" else ("CSV+xlsx" if fmt == "both" else "CSV")
    print(f"\n{'=' * 60}")
    print(f"  Exporting final {label} from DB ...")
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
        help="Skip the final DB->file export step",
    )
    parser.add_argument(
        "--export-format", choices=["csv", "xlsx", "both"], default="xlsx",
        help=(
            "Format for the final per-table export from the DB. Default "
            "``xlsx`` (client deliverable). Pass ``both`` to also produce "
            "a CSV, or ``csv`` for the legacy CSV-only behaviour."
        ),
    )
    parser.add_argument(
        "--auction-mode", choices=["past", "upcoming", "both"], default="past",
        help=(
            "Which mode to run for event-auction sources (rms / bon / "
            "broadarrow / cac). ``past`` = sold lots with hammer prices "
            "(default, backwards-compatible). ``upcoming`` = currently-"
            "scheduled lots with pre-sale estimates only. ``both`` = run "
            "each supported source twice (once per mode). Sources without "
            "upcoming support are run only when --auction-mode allows "
            "their available mode."
        ),
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
    parser.add_argument(
        "--workers", type=int, default=_DEFAULT_PARALLEL_WORKERS,
        help=(
            f"Parallel scraper subprocesses (default {_DEFAULT_PARALLEL_WORKERS}). "
            "Pass 1 to run sequentially with live terminal output. "
            "Going above ~8 risks tripping shared-IP rate limits across sources."
        ),
    )
    parser.add_argument(
        "--coverage-check", action="store_true",
        help=(
            "Enable the field-coverage / row-count gate before each DB write. "
            "OFF by default for full runs because the seeded baselines were "
            "captured at one min_price/mode and false-trip on different "
            "populations. Use it on incremental small runs where the "
            "population matches the baseline."
        ),
    )
    args = parser.parse_args()

    # Create output dirs BEFORE the FileHandler tries to open the log file —
    # the handler crashes during basicConfig() if the parent dir doesn't
    # exist yet. Create both the hardcoded ``output/`` (for the run-wide log)
    # and the user-supplied ``--output-dir`` (for per-scraper CSVs).
    (ROOT / "output").mkdir(parents=True, exist_ok=True)
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(ROOT / "output" / f"run_all_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8"),
        ],
    )

    only = set(args.only) if args.only else None
    skip = set(args.skip) if args.skip else set()
    extra = ["--no-filter"] if args.no_filter else []
    extra += ["--output-dir", args.output_dir]

    # Coverage gate: OFF by default for run_all (full archive runs span
    # populations the seeded baselines weren't captured against, so the
    # gate false-trips). Caller opts in with --coverage-check; we forward
    # via env var because run_scraper.py reads OVERTAKE_SKIP_COVERAGE.
    import os as _os
    subprocess_env = dict(_os.environ)
    if not args.coverage_check:
        subprocess_env["OVERTAKE_SKIP_COVERAGE"] = "1"
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
    # ``--auction-mode`` filter: when a source's mode list contains the
    # past/upcoming keywords, narrow to whichever the caller asked for.
    # Sources whose modes are unrelated (e.g. classifieds' used/new/all)
    # or modes=None (daily/monthly sold-only scrapers) are unaffected.
    def _apply_auction_mode_filter(modes: list[str] | None) -> list[str] | None:
        if modes is None:
            return None
        has_pu = any(m in ("past", "upcoming") for m in modes)
        if not has_pu:
            return modes  # used/new/all-style modes are untouched
        if args.auction_mode == "both":
            return [m for m in modes if m in ("past", "upcoming")]
        # past or upcoming — return [requested] only if the source supports it
        return [args.auction_mode] if args.auction_mode in modes else []

    all_sources = _AUCTION_SOURCES + _CLASSIFIEDS_SOURCES
    plan: list[tuple[str, str | None, str]] = []
    for code, modes, desc in all_sources:
        if only and code not in only:
            continue
        if code in skip:
            continue
        filtered_modes = _apply_auction_mode_filter(modes)
        if filtered_modes == []:
            print(f"  skip {code:25s}  (no {args.auction_mode!r} mode support)")
            continue
        if filtered_modes:
            for m in filtered_modes:
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
    print(f"  Workers   : {args.workers} ({'sequential' if args.workers == 1 else 'parallel'})")
    print(f"  Coverage  : {'ON' if args.coverage_check else 'OFF (default for full runs)'}")
    print(f"  Auction   : --auction-mode={args.auction_mode}")
    print()

    # -- 3. Execute --------------------------------------------------------
    wall_start = time.time()
    results: list[tuple[str, int, float]] = []

    log_dir = ROOT / "output" / "scraper_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sequential = args.workers <= 1
    workers = max(1, args.workers)

    # Build all commands up front so the executor sees one homogenous queue.
    jobs = []
    for code, mode, _desc in plan:
        cmd = _build_cmd(
            code, mode,
            min_price=args.min_price,
            max_pages=args.max_pages,
            source_page_overrides=source_page_overrides,
            extra_args=extra,
        )
        jobs.append((code, mode, cmd))

    print(f"  Streaming live output: {'YES' if sequential else 'NO (per-source logs in output/scraper_logs/)'}")
    print()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_run_one, code, mode, cmd,
                       log_dir=log_dir, sequential=sequential,
                       env=subprocess_env)
            for code, mode, cmd in jobs
        ]
        for fut in as_completed(futures):
            label, rc, elapsed, _log_path = fut.result()
            results.append((label, rc, elapsed))

    # -- 4. Export final CSVs ---------------------------------------------
    if not args.no_export:
        _export_final(Path(args.output_dir), fmt=args.export_format)

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
