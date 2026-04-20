"""BaT-specific configuration."""

import random

from shared.config import get_headers

# BaT API
BAT_API_BASE = "https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter"
BAT_RESULTS_PER_PAGE = 36
BAT_SORT = "bd"  # bid descending

BAT_REFERERS = [
    "https://bringatrailer.com/auctions/results/",
    "https://bringatrailer.com/auctions/results/?sortby=bd",
    "https://bringatrailer.com/",
]


def get_api_headers() -> dict[str, str]:
    """BaT API request headers."""
    headers = get_headers(accept="*/*", referer=random.choice(BAT_REFERERS))
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Site"] = "same-origin"
    return headers


def get_page_headers() -> dict[str, str]:
    """BaT listing page request headers."""
    return get_headers(
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        referer=random.choice(BAT_REFERERS),
    )
