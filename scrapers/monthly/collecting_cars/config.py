"""Collecting Cars — Typesense multi_search + listing HTML."""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Browser-exposed search-only key (same family as Carbuckle's Typesense key).
TYPESENSE_API_KEY = os.getenv(
    "COLLECTING_CARS_TYPESENSE_KEY",
    "pHuIUBo3XGxHk9Ll9g4q71qXbTYAM2w1",
)
MULTI_SEARCH_URL = "https://dora.production.collecting.com/multi_search"
SITE_ORIGIN = "https://collectingcars.com"
LISTING_BASE = f"{SITE_ORIGIN}/for-sale"
RESULTS_REFERER = f"{SITE_ORIGIN}/"

COLLECTION = "production_cars"
PER_PAGE = 250

# Optional cap on listing HTML fetches (0 = fetch for every Typesense row returned).
MAX_DETAILS = int(os.getenv("COLLECTING_CARS_MAX_DETAILS", "0"))
