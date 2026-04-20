"""Constants for DuPont REGISTRY Live."""

BASE_URL = "https://live.dupontregistry.com"
LISTINGS_ENDPOINT = "/api/auctions/listings"
DEFAULT_PAGE_SIZE = 21
TYPE_SOLD = "sold"
SORT_BID_HIGH = "bid_high"
# NextAuth credential callback expects a callback URL on the same origin.
LISTINGS_CALLBACK_URL = f"{BASE_URL}/listings/sold/filter:sort=bid_high"
