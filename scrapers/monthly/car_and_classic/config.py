"""Constants for Car & Classic auctions."""

SITE_ORIGIN = "https://www.carandclassic.com"

# /us/search hits the same Inertia search component as /auctions/results
# but exposes the public price-floor filter (``min_price``) which the older
# /auctions/results URL does not. Switching shaves the post-filter waste —
# the upstream now returns only matching rows. Note: the underlying price
# values in the response are still GBP-pence regardless of the /us/ prefix
# (the prefix only affects UI display formatting), so the ``min_price``
# value sent in the query string is also GBP-pence.
AUCTION_RESULTS_URL = f"{SITE_ORIGIN}/us/search"
RESULTS_REFERER = AUCTION_RESULTS_URL

# Fixed params that scope the search to ended auctions (vs upcoming or
# classified ads). Per-call params (page, min_price) are added on top.
SEARCH_BASE_PARAMS: dict[str, str] = {
    "auction_state[0]": "ended",
    "listing_type_ex": "auction",
}

INERTIA_SEARCH_COMPONENT = "search/index/Page"
INERTIA_DETAIL_COMPONENT = "listings/auctions/show/Page"

# Minimal partial props (matches browser XHR).
SEARCH_PARTIAL_DATA = "searchResults,searchParams,errors,boostSuggestion"
LISTING_PARTIAL_DATA = "listing"
