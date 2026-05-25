"""Mecum.com — Algolia search (public browser credentials)."""

import os
import time

MECUM_SITE_ORIGIN = "https://www.mecum.com"

# Public search-only keys from the Mecum Next.js client (override if Algolia credentials rotate).
ALGOLIA_APP_ID = os.getenv("MECUM_ALGOLIA_APP_ID", "U6CFCQ7V52")
ALGOLIA_API_KEY = os.getenv("MECUM_ALGOLIA_API_KEY", "0291c46cde807bcb428a021a96138fcb")

ALGOLIA_QUERIES_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"

ALGOLIA_AGENT = (
    "Algolia for JavaScript (5.28.0); Search (5.28.0); Browser; instantsearch.js (4.79.0); "
    "react (19.2.1); react-instantsearch (7.16.0); react-instantsearch-core (7.16.0); "
    "next.js (15.5.7); JS Helper (3.26.0)"
)

LOT_INDEX_NAME = "wp_posts_lot_highest_bid_desc"
# Algolia caps at 1000; the browser uses 24. Bumping to 96 cuts the
# /queries round-trip count by 4x (~9,200 pages -> ~2,300) while still
# fitting comfortably in one response payload.
HITS_PER_PAGE = 96

# Upcoming-auction index — the browser uses this when viewing a future
# auction's published catalog. Sorted by ``feature_sort_meta`` ascending
# (sticky / featured lots first, then numeric lot order), which is fine
# for our purposes since we paginate the whole auction anyway.
UPCOMING_LOT_INDEX_NAME = "wp_posts_lot_feature_sort_asc"

# Auctions overview page — we scrape it to learn which upcoming events
# have published their lot catalogue ("View Lots" anchor). Far-future
# events render no anchor and are skipped.
AUCTIONS_OVERVIEW_PATH = "/auctions/"

# Mirrors the site filter: past / ended lots with sold facet + time window.
def run_date_timestamp_ceiling() -> int:
    """Unix timestamp upper bound for ``taxonomies.run_date.timestamp`` clause (rolling)."""
    return int(time.time()) + 86400 * 800
