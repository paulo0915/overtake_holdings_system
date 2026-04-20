"""PCarMarket-specific configuration."""

PCAR_BASE_URL = "https://pcarmarket.com"
PCAR_API_BASE = "https://pcarmarket.com/api"
PCAR_RESULTS_PER_PAGE = 24

# Status mapping — use status_class field from API
STATUS_CLASS_MAP = {
    "sold": "Sold",
    "unsold": "Unsold",
    "cancelled": "Withdrawn",
    "withdrawn": "Withdrawn",
}
