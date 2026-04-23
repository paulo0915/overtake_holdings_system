"""Cars.com — Apollo GraphQL (SRP list + VDP detail) classifieds listing site.

The HTML detail page (``/vehicledetail/<listingId>/``) is behind Akamai bot
protection and returns 403 to both raw httpx and curl_cffi Chrome-impersonation.
But the Apollo GraphQL endpoint at ``graph.cars.com/graphql/api`` accepts the
request with just the public anonymous API key + platform/trip headers, so we
fetch list + detail via GraphQL and never touch the HTML VDP.
"""

SITE_ORIGIN = "https://www.cars.com"
GRAPHQL_URL = "https://graph.cars.com/graphql/api"

# Public Apollo anonymous key, lifted from the SRP's JS bundle. Rotates rarely.
API_KEY = "5rrmnWVl1MDzPcEnDvEp3Pu101IGXEGo"

# Trip-ID is a UUID the browser generates per session — any UUIDv4 works.
# We fix one for reproducibility but generate per-scrape in api_client.
DEFAULT_TRIP_ID = "7b31b1ff-5f73-4647-a71b-5ea3f0e4ab1d"

# SRP pagination. Site default is 22; anything above ~100 gets rejected.
DEFAULT_PAGE_SIZE = 22

# Cars.com caps total results at 10,000 regardless of filter. With a
# ``--min-price`` above ~$50k the luxury pool stays well under this.
HARD_RESULT_CAP = 10_000

# Headers shared by list + detail — Accept is JSON, Origin + Referer are
# required; missing any one triggers a 400 "Bad Request" response.
_SHARED_HEADERS = {
    "Accept": "application/graphql-response+json, application/json",
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
    "X-Cars-Platform": "cars_responsive",
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}/",
}


def build_headers(trip_id: str) -> dict[str, str]:
    """Return request headers, slotting the per-run trip-id in."""
    return {**_SHARED_HEADERS, "X-Cars-Trip-Id": trip_id}


# Canonical VDP URL — uses listingId (UUID), not the numeric stockId.
DETAIL_URL_TEMPLATE = "{origin}/vehicledetail/{listing_id}/"


# ── Queries ────────────────────────────────────────────────────────────────

# Search Results Page query. ``srpSearch`` accepts a required
# ``selectedSearchFilters`` array plus optional ``page`` / ``pageSize`` /
# ``sort`` (enum literal). The result union needs an inline fragment on
# ``SrpListingGridCard`` to pull useful fields — other union members are ad
# slots and editorial cards that we discard.
#
# ``analytics.context`` is a stringified JSON blob carrying the structured
# row fields (vin, price, year, make, model, mileage, trim, bodyStyle,
# stockType, seller.zip, primaryThumbnail). Dealer name is NOT in the
# context — we get it from the per-row detail call.
#
# The ``sort: LIST_PRICE_DESC`` is an enum literal (not a string) and must
# be embedded in the query text rather than passed as a variable.
LIST_QUERY = """
query SearchResultsPageSearch(
  $filters: [SelectedSearchFilterInput!]!
  $page: Int
  $pageSize: Int
) {
  srpSearch(
    selectedSearchFilters: $filters
    page: $page
    pageSize: $pageSize
    sort: LIST_PRICE_DESC
  ) {
    metadata { totalListings page pageSize totalPages }
    results {
      __typename
      ... on SrpListingGridCard {
        listingId
        analytics { context }
      }
    }
  }
}
""".strip()

# VDP (detail) query — ``listingDetails(listingId: UUID!)`` returns the full
# payload including seller-written ``adDescription`` (HTML), colors, engine
# description, and dealer contact info.
DETAIL_QUERY = """
query ListingDetails($listingId: UUID!) {
  listingDetails(listingId: $listingId) {
    listedAt
    inventory {
      vin
      listPrice
      mileage
      stockNumber
      certifiedPreowned
      inventoryDisplay {
        listPrice
        modelYear
        make
        model
        trim
        stockType
        bodyStyle
        imageUrls
        interiorColor
        exteriorColor
        transmissionDescription
        drivetrainDescription
        mileage
        engineDescription
        fuelType
        adDescription
        dealerVehicleUrl
        cylinderCount
        doorCount
        seatCount
      }
    }
    dealer {
      id
      name
      address { city state }
      phones { areaCode localNumber }
    }
  }
}
""".strip()
