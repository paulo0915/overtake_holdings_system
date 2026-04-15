"""Cars & Bids specific configuration."""

from shared.config import MIN_PRICE_USD, MAX_CONCURRENT_REQUESTS, REQUEST_DELAY_SECONDS

CAB_BASE_URL = "https://carsandbids.com"
CAB_API_BASE = "https://carsandbids.com/v2/autos/auctions"
CAB_RESULTS_PER_PAGE = 50

# Transmission mapping: C&B uses integers
TRANSMISSION_MAP = {
    1: "automatic",
    2: "manual",
}

# Status mapping: C&B uses strings
STATUS_MAP = {
    "sold": "Sold",
    "reserve_not_met": "Unsold",
    "bid_not_met": "Unsold",
    "cancelled": "Withdrawn",
    "canceled": "Withdrawn",
    "withdrawn": "Withdrawn",
    "closed": "Unsold",
}
