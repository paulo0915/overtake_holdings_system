"""Barrett-Jackson — previous-docket (past) + docket (upcoming) APIs."""

SITE_ORIGIN = "https://www.barrett-jackson.com"
RESULTS_API_PATH = "/api/previous-docket-results"
UPCOMING_DOCKET_API_PATH = "/api/docket"

DEFAULT_PAGE_SIZE = 48
DEFAULT_RESULT_TYPE = "Vehicles"
DEFAULT_SORT = "price_desc"

# /api/docket parameter used by the browser when viewing a future auction's
# preview docket. ``select_preview`` flag returns lots that are confirmed
# but haven't crossed the block yet (vs the day-of-auction status).
UPCOMING_EVENT_STATUS = "select_preview"
