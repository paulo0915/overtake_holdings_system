"""Constants for Car & Classic auctions."""

SITE_ORIGIN = "https://www.carandclassic.com"
AUCTION_RESULTS_URL = f"{SITE_ORIGIN}/auctions/results"
RESULTS_REFERER = AUCTION_RESULTS_URL

INERTIA_SEARCH_COMPONENT = "search/index/Page"
INERTIA_DETAIL_COMPONENT = "listings/auctions/show/Page"

# Minimal partial props (matches browser XHR).
SEARCH_PARTIAL_DATA = "searchResults,searchParams,errors,boostSuggestion"
LISTING_PARTIAL_DATA = "listing"
