"""Cars & Bids specific configuration."""

CAB_BASE_URL = "https://carsandbids.com"
CAB_API_BASE = "https://carsandbids.com/v2/autos/auctions"
CAB_RESULTS_PER_PAGE = 50

# Transmission mapping: C&B uses integers
TRANSMISSION_MAP = {
    1: "Automatic",
    2: "Manual",
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
