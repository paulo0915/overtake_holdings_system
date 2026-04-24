# Overtake Holdings — Auction & Classifieds Scraping System

Scrapers + Supabase ingestion pipeline for **27 sources** — 19 auction venues
and 8 dealer-classifieds sites. Built for Overtake Holdings' collector-car
market-data platform.

## What it does

- Scrapes auction listings (past and upcoming) from 19 auction sources.
- Scrapes classifieds listings from 8 dealer marketplaces, with price-change
  / removed / relisted lifecycle tracking.
- Normalises every listing into a single canonical column shape
  (`AUCTION_COLUMNS` or `CLASSIFIEDS_COLUMNS`).
- Upserts rows into Supabase (dedup on `listing_url` for auctions; on
  VIN + dealer + URL for classifieds).
- Exports per-source CSV + Excel to `output/` for spot-checking.

## Quickstart

1. Install deps:
   ```
   pip install -r requirements.txt
   python -m playwright install firefox chromium
   ```
2. Copy `.env.example` → `.env` and fill in:
   - `SUPABASE_URL` — project URL
   - `SUPABASE_KEY` — **service_role** secret key (not the anon/publishable key)
3. Create the tables in Supabase — run these SQL files in the SQL editor:
   - `db/migrations/001_create_auctions.sql`
   - `db/migrations/002_create_classifieds.sql`
   - `db/migrations/003_create_proxies.sql` *(only if running the AutoTrader.com scraper)*
4. For AutoTrader.com only, seed residential proxies:
   - Paste `db/seeds/001_proxies_iproyal.sql` / `002_proxies_iproyal_intl.sql` into
     the SQL editor (or load your own — the codebase accepts both
     `host:port:user:pass` shorthand and `http://user:pass@host:port` URL form).
5. Run a scraper:
   ```
   python scripts/run_scraper.py --source bat --max-pages 1
   ```
   Rows go to Supabase by default. Add `--no-store-db` to skip the DB write.

## Sources

### Auction — Daily (6) — `scrapers/daily/`
- `bat` — Bring a Trailer
- `cab` — Cars & Bids
- `pcar` — PCarMarket
- `dpl` — DuPont Registry Live
- `sbm` — Sotheby's Motorsport
- `hag` — Hagerty Marketplace

### Auction — Monthly (6) — `scrapers/monthly/`
- `mbm` — The MB Market
- `sbx` — SBX Cars
- `cbk` — Carbuckle
- `cc` — Collecting Cars
- `tmkt` — The Market (themarket.co.uk)
- `cac` — Car & Classic

### Auction — Event-based (7) — `scrapers/event/`
- `rms` — RM Sotheby's (supports `--mode past|upcoming`)
- `bon` — Bonhams Cars (supports `--mode past|upcoming`)
- `broadarrow` — Broad Arrow (supports `--mode past|upcoming`)
- `gooding` — Gooding & Company
- `mecum` — Mecum Auctions
- `barrett` — Barrett-Jackson
- `worldwide` — Worldwide Auctioneers

### Classifieds (8) — `scrapers/classifieds/`
- `dpr` — DuPont Registry (GraphQL + HTML detail)
- `cars` — Cars.com (GraphQL `srpSearch` + `listingDetails`)
- `cg` — CarGurus (Remix `_data` loader — `--mode used|new`)
- `tc` — TrueCar (GraphQL `getMarketplaceSearch` + VDP Apollo state — `--mode used|new`)
- `uc` — UsedCars.com (Dealix REST API)
- `atca` — AutoTrader.ca (Next.js `_next/data` loader — `--mode used|new`)
- `je` — JamesEdition (HTML-only)
- `atcom` — AutoTrader.com (Akamai v2 — requires residential proxies, browser-only;
  `--mode used|new|all`)

## CLI reference

```
python scripts/run_scraper.py --source <name> [options]

--source NAME         Required. One of the 27 source codes above.
--mode STRING         past / upcoming (event auctions)
                      used / new / all (CarGurus, TrueCar, AutoTrader.ca, AutoTrader.com)
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
| `bat`, `cab`, `pcar`, `hag`, `dpl`, `sbm`, `mbm`, `cbk`, `cc`, `tmkt`, `cac`, `mecum`, `barrett` | HTTP pages | `--max-pages 3` = 3 paginated API pages |
| `sbx`, `worldwide`, `gooding`, `broadarrow` (upcoming) | Lot count | `--max-pages 50` = 50 lots |
| `rms`, `bon`, `broadarrow` (past) | Auction count | `--max-pages 2` = 2 auctions |
| All 8 classifieds | API / SRP pages | page size ~20–30 listings depending on source |

## Architecture

Every scraper has the same 5-file layout:

```
scrapers/<tier>/<name>/
    config.py         — domain, endpoint constants, sort params, API keys
    api_client.py     — httpx / curl_cffi / Playwright calls with @retry
    listing_parser.py — raw response → normalized dict
    record_builder.py — normalized dict → AUCTION_COLUMNS / CLASSIFIEDS_COLUMNS row
    scraper.py        — orchestrator, inherits shared/base_scraper.BaseScraper
```

Shared layer — `shared/`:

- `schemas.py` — canonical `AUCTION_COLUMNS` + `CLASSIFIEDS_COLUMNS` lists
- `base_scraper.py` — ABC every scraper inherits
- `db.py` — Supabase upsert (`insert_auction_records`, `upsert_classified_record`,
  `mark_removed_classifieds`)
- `export.py` — CSV + Excel writers, `enrich_records` (derives `Auction_status`,
  normalizes VIN / engine / country / transmission)
- `fx_converter.py` — Frankfurter-backed USD conversion, date-aware
- `normalizer.py` — mileage, engine, transmission, VIN, country, date helpers
- `vehicle_filter.py` — optional make/model whitelist
- `proxies.py` — residential-proxy rotation against the `proxies` Supabase table
  (used by the AutoTrader.com scraper to bypass Akamai)

## Supabase schema

Three tables: `auctions`, `classifieds`, `proxies`. See `db/migrations/` for the
exact DDL.

### `auctions`
- `record_id` — UUID, server-generated
- `listing_url` — UNIQUE, required (upsert conflict key)
- `source` — domain string, required
- `auction_status` — `"Completed"` (hammer settled) or `"Upcoming"` (not yet)
- `sold_status` — `"Sold"` / `"Unsold"` / `"Withdrawn"` / `null` (for upcoming)
- **Lifecycle**: scraping an upcoming lot and later scraping the same URL as past
  **overwrites** the pre-event row with the post-event hammer data.

### `classifieds`
- Same shape as `auctions` plus:
  - `dealer_name`, `asking_price`, `asking_price_usd`, `fx_rate_used`
  - `listing_status` — `"Active"` / `"Removed"` / `"Relisted"`
  - `first_seen_date`, `last_seen_date`
  - `previous_asking_price`, `last_price_change_date`
- **Dedup key**: VIN + `dealer_name` + `listing_url` (all three must match).
- **Lifecycle** (per-scrape):
  - New URL → `inserted`, status `Active`
  - Same URL + same price → `skipped`, refresh `last_seen_date` + `scraped_at`
  - Same URL, new price → `updated`, shift old price to `previous_asking_price`,
    set `last_price_change_date`
  - After each scrape: any `Active` row with this source that wasn't seen this pass
    gets flipped to `Removed`
- For multi-mode sources (`cg`/`tc`/`atca`/`atcom` with used vs new), the `source`
  column is suffixed (e.g. `truecar.com/used`) so used/new lifecycles don't
  cross-contaminate.

### `proxies`
- `id`, `proxy`, `is_bad`
- `proxy` stores either `host:port:user:pass` vendor shorthand or standard URL
  form — `shared/proxies.py::_parse` normalises on load.
- Used by `atcom` to warm Akamai cookies through a residential IP; `is_bad` flips
  automatically on proxy-level failures (connection refused, auth errors, etc.).

## Data export

Dump either / both tables to CSV:

```
python scripts/export_db_csv.py                       # both → output/<table>_YYYY-MM-DD.csv
python scripts/export_db_csv.py --table auctions      # just one
python scripts/export_db_csv.py --out custom_path.csv # custom location (single table only)
```

Output is UTF-8 without BOM (stdlib `csv.DictReader`, awk, Node, Go compatible).

## Maintenance

### `backfill_vin_and_descriptions.py`

One-shot script to apply shared-layer fixes to existing DB rows without
re-scraping:

- VIN normalization (null placeholders like `.`, `001`, `DK28`; strip Bonhams
  `ENGINENO` concat suffixes).
- Collecting Cars boilerplate truncation (English T&Cs + Spanish import-tax
  disclaimer).

```
python scripts/backfill_vin_and_descriptions.py           # dry run
python scripts/backfill_vin_and_descriptions.py --apply   # write
```

## Development notes

- Python 3.11+
- Windows: `curl_cffi` needs the Selector event loop; Playwright (used by
  `cab` and `atcom`) needs Proactor. `run_scraper.py` auto-picks based on
  `--source`.
- `atcom` needs system **Chrome** OR bundled Firefox 146+ installed for
  Playwright. It also needs residential proxies (ideally non-US — JP/PL/PH
  pass Akamai's IP-reputation check; US residentials are heavily flagged).
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
- **JamesEdition**: dealers often leave the "About This Car" block empty —
  ~60% of listings have dealer prose, the rest have no description at all.
- **AutoTrader.com**: `int_color` isn't in the REST feed (source-side gap);
  ~5% of listings have no dealer description (the "More from the seller"
  section is simply absent when the dealer hasn't written one).

## Operations

**Full daily sweep (auctions):**
```
for src in bat cab pcar hag dpl sbm; do
    python scripts/run_scraper.py --source $src --max-pages 0
done
```

**Weekly upcoming refresh** (event scrapers that support it):
```
for src in rms bon broadarrow; do
    python scripts/run_scraper.py --source $src --mode upcoming --max-pages 0
done
```

**Classifieds sweep (picks up price changes + removals):**
```
for src in dpr cars uc je; do
    python scripts/run_scraper.py --source $src --max-pages 0
done
# mode-scoped sources need both variants
for src in cg tc atca; do
    python scripts/run_scraper.py --source $src --mode used --max-pages 0
    python scripts/run_scraper.py --source $src --mode new  --max-pages 0
done
# atcom — slow because of Akamai warmup; run last
python scripts/run_scraper.py --source atcom --mode all --max-pages 0
```

**One-time historical backfill (auctions):**
```
python scripts/run_scraper.py --source bon --mode past --max-pages 0
python scripts/run_scraper.py --source rms --mode past --max-pages 0
python scripts/run_scraper.py --source gooding --max-pages 0
# etc.
```
