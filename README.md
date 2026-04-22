# Overtake Holdings — Auction & Classifieds Scraping System

Scrapers + Supabase ingestion pipeline for 19 auction sources. Built for
Overtake Holdings' collector-car market-data platform.

## What it does

- Scrapes auction listings (past and upcoming) from 19 sources.
- Normalises every listing into a single `AUCTION_COLUMNS` shape.
- Upserts rows into Supabase (dedup on `listing_url`; pre-event rows overwrite
  cleanly when the auction finishes).
- Exports per-source CSV + Excel to `output/` for spot-checking.

## Quickstart

1. Install deps:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` → `.env` and fill in:
   - `SUPABASE_URL` — project URL
   - `SUPABASE_KEY` — **service_role** secret key (not the anon/publishable key)
3. Create the tables in Supabase — run these two SQL files in the SQL editor:
   - `db/migrations/001_create_auctions.sql`
   - `db/migrations/002_create_classifieds.sql`
4. Run a scraper:
   ```
   python scripts/run_scraper.py --source bat --max-pages 1
   ```
   Rows go to Supabase by default. Add `--no-store-db` to skip the DB write.

## Sources

### Daily (6) — `scrapers/daily/`
- `bat` — Bring a Trailer
- `cab` — Cars & Bids
- `pcar` — PCarMarket
- `dpl` — DuPont Registry Live
- `sbm` — Sotheby's Motorsport
- `hag` — Hagerty Marketplace

### Monthly (6) — `scrapers/monthly/`
- `mbm` — The MB Market
- `sbx` — SBX Cars
- `cbk` — Carbuckle
- `cc` — Collecting Cars
- `tmkt` — The Market (themarket.co.uk)
- `cac` — Car & Classic

### Event auctions (7) — `scrapers/event/`
- `rms` — RM Sotheby's (supports `--mode past|upcoming`)
- `bon` — Bonhams Cars (supports `--mode past|upcoming`)
- `broadarrow` — Broad Arrow (supports `--mode past|upcoming`)
- `gooding` — Gooding & Company
- `mecum` — Mecum Auctions
- `barrett` — Barrett-Jackson
- `worldwide` — Worldwide Auctioneers

## CLI reference

```
python scripts/run_scraper.py --source <name> [options]

--source NAME         Required. One of the 19 source codes above.
--mode past|upcoming  For RM / Bonhams / Broad Arrow only. Default: past.
--max-pages N         Cap scrape size (0 = unlimited). See Semantics below.
--min-price USD       Override shared config default.
--max-concurrent N    Override shared concurrency (default per source).
--use-filter          Apply the vehicle filter from markets_to_track_closely.xlsx.
--no-store-db         Skip Supabase write (CSV/Excel export only).
--output-dir DIR      Default: ``output``.
-v, --verbose         DEBUG logs.
```

### `--max-pages` semantics by scraper

Units differ because source pagination differs:

| Scrapers | Unit | Example |
|---|---|---|
| bat, cab, pcar, hag, dpl, sbm, mbm, cbk, cc, tmkt, cac, mecum, barrett | HTTP pages | `--max-pages 3` = scrape 3 paginated API pages |
| sbx, worldwide, gooding, broadarrow (upcoming) | Lot count | `--max-pages 50` = stop after 50 lots |
| rms, bon, broadarrow (past) | Auction count | `--max-pages 2` = process first 2 auctions |

## Architecture

Every scraper has a 5-file layout under its folder:

```
scrapers/<tier>/<name>/
    config.py         — domain, endpoint constants, sort params, API keys
    api_client.py     — httpx/async calls with @retry for transient failures
    listing_parser.py — raw response → normalized dict
    record_builder.py — normalized dict → AUCTION_COLUMNS row
    scraper.py        — orchestrator, inherits shared/base_scraper.BaseScraper
```

Shared layer — `shared/`:

- `schemas.py` — the canonical `AUCTION_COLUMNS` + `CLASSIFIEDS_COLUMNS` lists
- `base_scraper.py` — ABC every scraper inherits
- `db.py` — Supabase upsert (`insert_auction_records` / `upsert_classified_record`)
- `export.py` — CSV + Excel writers, `enrich_records` (derives `Auction_status`)
- `fx_converter.py` — Frankfurter-backed USD conversion, date-aware
- `normalizer.py` — mileage, engine, transmission, country, date helpers
- `vehicle_filter.py` — optional make/model whitelist

## Supabase schema

Two tables: `auctions` and `classifieds`. Both use an auto-generated UUID
`record_id` primary key and `listing_url` as the upsert conflict key. See
`db/migrations/` for the exact DDL.

**Key columns on `auctions`:**
- `record_id` — UUID, server-generated
- `listing_url` — UNIQUE, required (dedup key)
- `source` — domain string, required
- `auction_status` — `"Completed"` (hammer settled) or `"Upcoming"` (not yet)
- `sold_status` — `"Sold"` / `"Unsold"` / `"Withdrawn"` / `null` (for upcoming)
- Lifecycle: scraping an upcoming lot and later scraping the same URL as past
  **overwrites** the pre-event row with the post-event hammer data.

## Development notes

- Python 3.11+
- Windows: curl_cffi needs the Selector event loop; Playwright (used by C&B)
  needs Proactor. `run_scraper.py` auto-picks based on `--source`.
- Logs rotate at 5 MB × 5 backups in `logs/scraper.log`.
- `output/` holds per-source CSVs + XLSXs for verification. Not required — DB
  is the authoritative sink.

## Known upstream data limits

These are source-side gaps, not parser bugs:

- **Worldwide Auctioneers**: `/car-search/` POST returns shallow cards with
  no colour / engine / transmission. Those columns stay null.
- **RM Sotheby's**: detail pages only expose `Chassis No.`, `Engine No.`,
  `Registration`, `Location`. No structured colour / transmission labels.
- **Sealed / single-lot auctions** (RM `MF26`, `DL26`): price is never
  published for these — sold rows get filtered out at build time.

## Operations

**Full daily sweep:**
```
for src in bat cab pcar hag dpl sbm; do
    python scripts/run_scraper.py --source $src --max-pages 0
done
```

**Weekly upcoming refresh** (3 event scrapers that support it):
```
for src in rms bon broadarrow; do
    python scripts/run_scraper.py --source $src --mode upcoming --max-pages 0
done
```

**One-time historical backfill:**
```
python scripts/run_scraper.py --source bon --mode past --max-pages 0
python scripts/run_scraper.py --source rms --mode past --max-pages 0
python scripts/run_scraper.py --source gooding --max-pages 0
# etc.
```
