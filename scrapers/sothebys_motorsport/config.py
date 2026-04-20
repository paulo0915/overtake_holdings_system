"""Constants for Sotheby's Motorsport (NextAuth + listings API + Next.js data)."""

BASE_URL = "https://sothebysmotorsport.com"

# NextAuth credential callback expects a callback URL on the same origin.
LISTINGS_CALLBACK_URL = f"{BASE_URL}/"

LISTINGS_ENDPOINT = "/api/auctions/listings"

# Observed query params from the web app.
DEFAULT_PAGE_SIZE = 15

TYPE_SOLD = "sold"
SORT_BID_HIGH = "bid_high"
MERGE_LIVE_AND_UPCOMING = False

