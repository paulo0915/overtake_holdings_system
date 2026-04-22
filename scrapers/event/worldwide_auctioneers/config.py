"""Worldwide Auctioneers listings subdomain (WordPress + WooCommerce)."""

MAIN_ORIGIN = "https://worldwideauctioneers.com"
LISTINGS_ORIGIN = "https://listings.worldwideauctioneers.com"
RESULTS_PATH = "/results/"
CAR_SEARCH_PATH = "/car-search/"
INVENTORY_PATH = "/inventory/"

# POST ``/car-search/`` — field names from DevTools (``application/x-www-form-urlencoded``).
# ``auctionid: 0`` + ``make: All`` = all auctions / all makes (full catalog filter).
CAR_SEARCH_ALL_LISTINGS_BODY: dict[str, str] = {
    "search": "1",
    "auctionid": "0",
    "lotnumber": "",
    "make": "All",
    "startyear": "",
    "model": "",
    "yearend": "",
}
