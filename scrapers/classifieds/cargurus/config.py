"""CarGurus — Remix app, POST SRP paginator + deferred-JSON detail endpoint.

Two flavours of listing, selected via ``?srpVariation=``:
    DEFAULT_SEARCH    → used + CPO cars (``LISTING_USED_STANDARD`` tiles)
    NEW_CAR_SEARCH    → new cars only  (``LISTING_NEW_STANDARD`` tiles)

Both paginate the same way. The detail page reuses the listing-id from the
list tiles and is fetched via the Remix loader route — no HTML scrape needed.
"""

SITE_ORIGIN = "https://www.cargurus.com"
SEARCH_PATH = "/search"

# Remix "data" route names. The SRP route (``routes/($intl).search``) is what
# the browser hits for paginated JSON; the detail route is for single listings.
SEARCH_DATA_ROUTE = "routes/($intl).search"
DETAIL_DATA_ROUTE = "routes/($intl).details.$listingId"

# 24 tiles per page, a mix of listings + a couple of merch cards we skip.
DEFAULT_PAGE_SIZE = 24

# Used vs New — selected by ``--mode`` flag in scraper.py and mapped here.
MODE_USED = "used"
MODE_NEW = "new"
_SRP_VARIATION = {
    MODE_USED: "DEFAULT_SEARCH",
    MODE_NEW: "NEW_CAR_SEARCH",
}


def srp_variation(mode: str) -> str:
    """Map ``used`` / ``new`` → the site's srpVariation query param value."""
    return _SRP_VARIATION.get(mode, _SRP_VARIATION[MODE_USED])


# Detail page URL — used as the canonical ``Listing_url`` we store. The
# ``?resultSetId=…`` tracking query seen in the browser isn't required to
# open the listing, so we drop it for a clean deduplication key.
DETAIL_URL_TEMPLATE = "{origin}/details/{listing_id}"


# Tile types we consume. Everything else (``MERCH_C2D``,
# ``MERCH_DEALERSHIP_MODE``, sponsored cards) we ignore.
LISTING_TILE_TYPES = frozenset({"LISTING_USED_STANDARD", "LISTING_NEW_STANDARD"})
