"""TrueCar — Apollo GraphQL list + Next.js ``__NEXT_DATA__`` detail scrape.

Two condition flavours driven by ``filters.condition`` on the same query:
    USED → used + CPO cars
    NEW  → new cars

Both use the same endpoint, query, sort, and pagination shape.

The detail page is a Next.js-rendered VDP at ``/used-cars-for-sale/listing/<vin>/``.
The seller-written description (``sellerNotes``) isn't exposed on the list
response, so we fetch the HTML and pull it out of the ``__APOLLO_STATE__``
blob inside ``<script id="__NEXT_DATA__">``.
"""

SITE_ORIGIN = "https://www.truecar.com"
GRAPHQL_URL = f"{SITE_ORIGIN}/graphql"

# 30 listings per page matches the SRP default. Anything above ~60 gets
# rejected upstream.
DEFAULT_PAGE_SIZE = 30

# TrueCar caps paginated results around 10k regardless of ``totalCount``.
HARD_RESULT_CAP = 10_000

# --- Mode selector -----------------------------------------------------------
MODE_USED = "used"
MODE_NEW = "new"

_CONDITION = {
    MODE_USED: "USED",
    MODE_NEW: "NEW",
}


def condition_for_mode(mode: str) -> str:
    """Map ``used``/``new`` → the GraphQL ``filters.condition`` enum value."""
    return _CONDITION.get(mode, _CONDITION[MODE_USED])


# VDP URL used both for user-facing ``Listing_url`` and for scraping the
# description. Same path for both used + new.
DETAIL_URL_TEMPLATE = "{origin}/used-cars-for-sale/listing/{vin}/"


# --- Required search radius --------------------------------------------------
# TrueCar requires ``withinRadius`` on every call. A huge radius around a
# coastal ZIP (90401 Santa Monica, from the captured request) covers the
# whole US. We keep the exact values the browser sends so no extra
# validation paths get triggered.
DEFAULT_POSTAL_CODE = "90401"
DEFAULT_RADIUS_MILES = 5000

# GraphQL operation + client headers. These must match what ``abp-frontend``
# sends — the missing ``apollographql-client-name`` header produces a 403.
OPERATION_NAME = "getMarketplaceSearch"
CLIENT_NAME = "abp-frontend"
AUTHORIZATION_MODE = "consumer"
