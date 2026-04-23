"""UsedCars.com — Dealix JSON API for list + detail.

Both endpoints live on ``apim.dealix.com`` and accept simple GET requests
with a fixed ``SourceID=33378`` (UsedCars.com's partner ID). No JS anti-bot,
no cookies required; just send ``Origin: https://www.usedcars.com`` and
``Referer: https://www.usedcars.com/`` headers so CORS doesn't reject us.

Inventory is used-only (carCondition is always "Used" on this feed), so
there's no ``--mode`` switch here.
"""

SITE_ORIGIN = "https://www.usedcars.com"
API_ORIGIN = "https://apim.dealix.com"
LIST_PATH = "/vis/v3/prod/get-inventory"
DETAIL_PATH = "/vis/v3/prod/get-vehicledetail"

# UsedCars.com's partner / source id for the Dealix API. Hard-coded into
# the site's JS bundle; rarely rotates.
SOURCE_ID = "33378"

# Azure API Management subscription key — the ``se-aw`` header. Without
# this the endpoint 401s with "missing subscription key". Lifted from the
# site's JS bundle; rotates rarely in practice.
SUBSCRIPTION_KEY = "d0fccada64f04a5180785e81fdd4bdec"

# 29 rows per page matches the browser's default.
DEFAULT_PAGE_SIZE = 29

# Santa Monica-adjacent zip with a huge radius: the Dealix API requires a
# zipcode + radius on every call, so we pick one that covers the entire US.
DEFAULT_ZIPCODE = "94506"
DEFAULT_RADIUS_MILES = 5000

# Site caps the SRP at ~10k results regardless of the ``inventoryCount``
# the filter facets report.
HARD_RESULT_CAP = 10_000

# UsedCars.com doesn't publish per-listing URLs we can link to directly —
# the site routes detail pages via client-side state. We synthesize a
# predictable search URL pointing at the VIN so the stored ``Listing_url``
# is clickable and unique.
DETAIL_URL_TEMPLATE = "{origin}/vehicle-detail/{used_car_id}/{vin}/"


# --- Field decoders ----------------------------------------------------------
# Dealix returns transmissions as single-letter codes — these are the ones
# that appear in the feed. Anything else we pass through unchanged so the
# shared normalizer still gets a chance at it.
TRANSMISSION_CODE_MAP = {
    "A": "Automatic",
    "M": "Manual",
}
