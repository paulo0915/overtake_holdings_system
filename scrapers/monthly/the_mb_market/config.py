"""Constants for The MB Market auction results."""

BASE_URL = "https://thembmarket.com"

# AJAX endpoint (POST).  ``action`` is in the query string.
AJAX_LIST_URL = f"{BASE_URL}/ajax/index.php?action=get_auction_list"

# Referer must match the results browse page (same-origin XHR).
RESULTS_REFERER = f"{BASE_URL}/browse-auctions/results"

DEFAULT_PAGE_SIZE = 16
LIST_TYPE_RESULTS = "results"

# First request: ``auction_last_page`` can be a placeholder; the JSON response
# echoes the real ``last_page`` count.
INITIAL_LAST_PAGE_PLACEHOLDER = 999
