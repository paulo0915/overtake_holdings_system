"""AutoTrader.com — REST JSON endpoint behind Akamai Bot Manager v2.

Access pattern:
    HTML SRP (warm)  /cars-for-sale/all-cars/ashburn-va?...
    JSON list        /rest/lsc/listing?... (xhr)
    HTML VDP         /cars-for-sale/vehicle/<listingId>

Anti-bot: Akamai Bot Manager v2. Raw httpx + all curl_cffi impersonation
profiles get a 200-status "page unavailable" stub (4444 bytes) from a cold
request. The hybrid we use:

  1. Playwright-stealth (headed) navigates the SRP and lets Akamai's JS
     challenge run → harvests ``_abck`` / ``bm_sz`` / ``ak_bmsc`` / ``bm_sv``
     / ``bm_s`` cookies after the sensor_data POST flips ``_abck`` to a
     "passed" state.
  2. Those cookies are copied into a curl_cffi AsyncSession (chrome124
     impersonation) and reused for all list + detail calls in the run.

Expected to work from a residential IP. Datacenter IPs (AWS/GCP/Anthropic
box) are pre-flagged by ASN regardless of TLS fingerprint or cookie state.
"""

SITE_ORIGIN = "https://www.autotrader.com"
LIST_REST_PATH = "/rest/lsc/listing"

# 25 listings per JSON page (AutoTrader fixes ``firstRecord`` stride at 25
# via its own pagination controls; anything else is rejected).
DEFAULT_PAGE_SIZE = 25

# Default area — Ashburn, VA zip 20146. ``searchRadius=0`` means "national"
# on this site (counter-intuitive but documented in their SRP UI: the zero
# radius toggles "nationwide" mode rather than local).
DEFAULT_ZIPCODE = "20146"
DEFAULT_CITY = "Ashburn"
DEFAULT_STATE = "VA"

# AutoTrader.com caps inventory enumeration around 10,000 records.
HARD_RESULT_CAP = 10_000

# --- Mode selector -----------------------------------------------------------
MODE_USED = "used"
MODE_NEW = "new"
MODE_ALL = "all"

# ``listingType`` query param values. "all-cars" is the site's default
# showing new + used + CPO in a single mixed feed.
_LISTING_TYPE = {
    MODE_USED: "USED",
    MODE_NEW: "NEW",
    MODE_ALL: None,  # No listingType param → all cars
}


def listing_type_for_mode(mode: str) -> str | None:
    return _LISTING_TYPE.get(mode, None)


# ``allListingType`` query param (used only when mode=all).
ALL_LISTING_TYPE = "all-cars"

# Detail page URL — AutoTrader exposes listing pages at ``/cars-for-sale/vehicle/<id>``.
DETAIL_URL_TEMPLATE = "{origin}/cars-for-sale/vehicle/{listing_id}"
