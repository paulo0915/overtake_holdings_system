"""Constants and env for The Market API."""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

SITE_ORIGIN = "https://www.themarket.co.uk"
CDN_ORIGIN = "https://cdn.themarket.co.uk"

RESULTS_REFERER_BASE = f"{SITE_ORIGIN}/auctions/results"

LISTINGS_RESULTS_URL = f"{SITE_ORIGIN}/api/listings/results"
LISTING_DETAIL_URL = f"{SITE_ORIGIN}/api/listings"

# Used only if HTML parsing fails (site layout change) or for tests.
ACCESS_TOKEN_FALLBACK = "zofLzhMgDXeT3nv3"

# Optional emergency override — normally the scraper reads the token from the results page HTML.
THE_MARKET_ACCESS_TOKEN = os.getenv("THE_MARKET_ACCESS_TOKEN")

# Smaller include keeps payloads smaller than the full SPA string; root ``data`` still has specs/estimates.
DETAIL_INCLUDE = "keyFacts,content"
