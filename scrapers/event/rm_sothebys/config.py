"""RM Sotheby's — past results and SearchLots API."""

SITE_ORIGIN = "https://rmsothebys.com"

# Year-inclusive range for ``/results/{year}`` discovery (newest first in scraper).
YEAR_END = 2026
YEAR_START = 2002

RESULTS_PATH = "/results/{year}"
SEARCH_LOTS_PATH = "/api/search/SearchLots"

# Paginated lot search (matches DevTools).
DEFAULT_PAGE_SIZE = 40

# POST JSON body template — ``Auction`` is uppercased slug (e.g. ``CH25``, ``MW26``).
SEARCH_LOTS_BODY_TEMPLATE: dict = {
    "LocationCountry": [],
    "Collection": None,
    "Auction": "",  # filled per request
    "Day": None,
    "SortBy": "Value",
    "CategoryTag": [],
    "StillForSaleOnly": False,
}
