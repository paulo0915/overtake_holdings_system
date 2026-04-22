"""Bonhams Cars — Typesense-backed auctions-search + Next.js per-auction/per-lot pages."""

SITE_ORIGIN = "https://cars.bonhams.com"

# Typesense search proxy (public read-only API key embedded on cars.bonhams.com).
TYPESENSE_HOST = "https://api01.bonhams.com"
TYPESENSE_SEARCH_PATH = "/search-proxy/multi_search"
TYPESENSE_API_KEY = "7YZqOyG0twgst4ACc2VuCyZxpGAYzM0weFTLCC20FQY"

AUCTIONS_PER_PAGE = 24
AUCTION_LOTS_PER_PAGE = 48

# Typesense search body for ended Bonhams Cars auctions — cars + memorabilia,
# ``biddingStatus:=EN`` means Ended. ``auctionType`` restricted to ONLINE/PUBLIC
# (excludes EXHIBITION). ``Auction`` filled per page; sorted newest first.
AUCTIONS_SEARCH_FILTER = (
    "(biddingStatus:=EN) && (brand:=[`bonhams-cars`]) "
    "&& (auctionType:=[`ONLINE`, `PUBLIC`])"
)
AUCTIONS_SORT_BY = "hammerTime.timestamp:desc,auctionTitle:desc"
