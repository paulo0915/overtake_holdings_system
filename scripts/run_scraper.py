"""CLI entry point for running scrapers.

Scraper structure
-----------------
scrapers/
    daily/     bat, cab, pcar, hag, dpl, sbm
    monthly/   mbm (The MB Market), sbx (SBX Cars), cbk (Carbuckle), …
    event/     mecum, barrett, broadarrow

Usage:
    python scripts/run_scraper.py --source bat
    python scripts/run_scraper.py --source bat --max-pages 2
    python scripts/run_scraper.py --source bat --use-filter
    python scripts/run_scraper.py --source hag --min-price 250000 --max-pages 3
    python scripts/run_scraper.py --source dpl --max-pages 1 --min-price 400000
    python scripts/run_scraper.py --source sbm --max-pages 1 --min-price 100000
    python scripts/run_scraper.py --source mbm --max-pages 2 --min-price 5000
    python scripts/run_scraper.py --source sbx --max-pages 5 --min-price 100000
    python scripts/run_scraper.py --source cbk --max-pages 2 --min-price 10000
    python scripts/run_scraper.py --source cc --max-pages 1 --min-price 0
    python scripts/run_scraper.py --source tmkt --max-pages 1 --min-price 0
    python scripts/run_scraper.py --source cac --max-pages 1 --min-price 0
    python scripts/run_scraper.py --source mecum --max-pages 1 --min-price 0
    python scripts/run_scraper.py --source barrett --max-pages 1 --min-price 0
    python scripts/run_scraper.py --source broadarrow --max-pages 1 --min-price 0
    # worldwide: --max-pages = max lots to export (0 = all on /inventory/); not "HTML pages"
    python scripts/run_scraper.py --source worldwide --max-pages 0 --min-price 0
    # rms: --max-pages = max auctions to process across years 2026→2002 (0 = all history)
    python scripts/run_scraper.py --source rms --max-pages 1 --min-price 0
    # rms upcoming: current /upcoming/ auctions with Sold_status='Upcoming'
    python scripts/run_scraper.py --source rms --mode upcoming --max-pages 0
    # bon: --max-pages = max ended Bonhams Cars auctions (0 = all ~593 auctions)
    python scripts/run_scraper.py --source bon --max-pages 1 --min-price 0
    # gooding: --max-pages = max lots to fetch (0 = all ~9k lots across every auction)
    python scripts/run_scraper.py --source gooding --max-pages 50 --min-price 0
"""

import asyncio
import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# curl_cffi requires the selector event loop on Windows (ProactorEventLoop lacks
# add_reader which curl_cffi's async internals need).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "scraper.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)


AVAILABLE_SOURCES = [
    "bat", "cab", "pcar", "hag", "dpl", "sbm",  # daily
    "mbm",  # monthly — The MB Market
    "sbx",  # monthly — SBX Cars
    "cbk",  # monthly — Carbuckle
    "cc",  # monthly — Collecting Cars
    "tmkt",  # monthly — The Market (themarket.co.uk)
    "cac",  # monthly — Car & Classic (carandclassic.com)
    "mecum",  # event — Mecum Auctions (mecum.com)
    "barrett",  # event — Barrett-Jackson (barrett-jackson.com)
    "broadarrow",  # event — Broad Arrow Auctions (broadarrowauctions.com)
    "worldwide",  # event — Worldwide Auctioneers (listings.worldwideauctioneers.com)
    "rms",  # event — RM Sotheby's (rmsothebys.com)
    "bon",  # event — Bonhams Cars (cars.bonhams.com)
    "gooding",  # event — Gooding & Company (goodingco.com)
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run auction/classified scraper")
    parser.add_argument(
        "--source", type=str, required=True, choices=AVAILABLE_SOURCES,
        help="Which source to scrape",
    )
    parser.add_argument("--min-price", type=int, default=None, help="Override MIN_PRICE_USD")
    parser.add_argument("--max-pages", type=int, default=0, help="Max API pages (0=unlimited, for testing)")
    parser.add_argument("--max-concurrent", type=int, default=None, help="Override concurrency")
    parser.add_argument("--output-dir", type=str, default="output", help="Output directory")
    parser.add_argument("--use-filter", action="store_true", help="Apply vehicle filter from tracking file")
    parser.add_argument(
        "--mode", type=str, default="past", choices=["past", "upcoming"],
        help="For event-auction scrapers: 'past' (default) or 'upcoming' lots",
    )
    parser.add_argument("--store-db", action="store_true", help="Write results to Supabase")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    from shared.config import MIN_PRICE_USD, MAX_CONCURRENT_REQUESTS, VEHICLE_FILTER_FILE

    # Vehicle filter (optional)
    vehicle_filter = None
    if args.use_filter:
        from shared.vehicle_filter import VehicleFilter
        vehicle_filter = VehicleFilter(VEHICLE_FILTER_FILE)
        logging.getLogger(__name__).info(f"Vehicle filter loaded from {VEHICLE_FILTER_FILE}")

    min_price = args.min_price if args.min_price is not None else MIN_PRICE_USD
    max_concurrent = args.max_concurrent if args.max_concurrent is not None else MAX_CONCURRENT_REQUESTS

    records = []

    # ── Daily scrapers ─────────────────────────────────────────────────────────
    if args.source == "bat":
        from scrapers.daily.bat.scraper import BaTScraper
        scraper = BaTScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "cab":
        from scrapers.daily.cars_and_bids.scraper import CarsAndBidsScraper
        scraper = CarsAndBidsScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        # C&B uses sync Playwright — run directly, not in asyncio
        records = scraper.run_sync()

    elif args.source == "pcar":
        from scrapers.daily.pcarmarket.scraper import PCarMarketScraper
        scraper = PCarMarketScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "hag":
        from scrapers.daily.hagerty.scraper import HagertyMarketplaceScraper
        scraper = HagertyMarketplaceScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "dpl":
        from scrapers.daily.dupont_live.scraper import DuPontLiveScraper
        scraper = DuPontLiveScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "sbm":
        from scrapers.daily.sothebys_motorsport.scraper import SothebysMotorsportScraper
        scraper = SothebysMotorsportScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    # ── Monthly scrapers ───────────────────────────────────────────────────────
    elif args.source == "mbm":
        from scrapers.monthly.the_mb_market.scraper import TheMbMarketScraper
        scraper = TheMbMarketScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "sbx":
        from scrapers.monthly.sbx_cars.scraper import SbxCarsScraper
        scraper = SbxCarsScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "cbk":
        from scrapers.monthly.carbuckle.scraper import CarbuckleScraper
        scraper = CarbuckleScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "cc":
        from scrapers.monthly.collecting_cars.scraper import CollectingCarsScraper
        scraper = CollectingCarsScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "tmkt":
        from scrapers.monthly.the_market.scraper import TheMarketScraper
        scraper = TheMarketScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "cac":
        from scrapers.monthly.car_and_classic.scraper import CarAndClassicScraper
        scraper = CarAndClassicScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "mecum":
        from scrapers.event.mecum.scraper import MecumScraper
        scraper = MecumScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "barrett":
        from scrapers.event.barrett_jackson.scraper import BarrettJacksonScraper
        scraper = BarrettJacksonScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "broadarrow":
        from scrapers.event.broad_arrow.scraper import BroadArrowScraper
        scraper = BroadArrowScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
            mode=args.mode,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "worldwide":
        from scrapers.event.worldwide_auctioneers.scraper import WorldwideAuctioneersScraper
        scraper = WorldwideAuctioneersScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "rms":
        from scrapers.event.rm_sothebys.scraper import RMSothebysScraper
        scraper = RMSothebysScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
            mode=args.mode,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "bon":
        from scrapers.event.bonhams.scraper import BonhamsScraper
        scraper = BonhamsScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
            mode=args.mode,
        )
        records = asyncio.run(scraper.run())

    elif args.source == "gooding":
        from scrapers.event.gooding.scraper import GoodingScraper
        scraper = GoodingScraper(
            min_price=min_price,
            output_dir=args.output_dir,
            max_concurrent=max_concurrent,
            max_pages=args.max_pages,
            vehicle_filter=vehicle_filter,
        )
        records = asyncio.run(scraper.run())

    # Store to Supabase if requested
    if args.store_db and records:
        from shared.db import insert_auction_records
        inserted = insert_auction_records(records)
        print(f"Stored {inserted} records to Supabase")

    print(f"\nDone! {len(records)} records exported to {args.output_dir}/")


if __name__ == "__main__":
    main()
