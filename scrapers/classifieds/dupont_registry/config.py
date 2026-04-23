"""duPont REGISTRY — Apollo/Next.js classifieds listing site."""

SITE_ORIGIN = "https://www.dupontregistry.com"

# Apollo GraphQL endpoint fronting their ES-backed search.
GRAPHQL_PATH = "/api/graphql"

# Per-page size on the site is 24 — using anything larger works but keeps us
# close to "real browser" behaviour that the endpoint optimises for.
DEFAULT_PAGE_SIZE = 24

# Field list hard-coded to match what cars-for-sale/all?sort=price_desc uses.
# Keeping it identical means the response shape is exactly what the site pulls
# — we don't need to re-parse anything unusual.
GRAPHQL_FIELDS = (
    "data.condition.code,"
    "data.dealer.preferredDealerStatus,data.dealer.name,data.dealer.alias,data.dealer.logo,data.dealer.website,"
    "data.updatedAt,data.createdAt,data.vin,data.stock,data.certified,"
    "data.locations.phone,data.locations.zipCode,data.locations.address,data.locations.metro,data.locations.metroAlias,"
    "data.mileage,data.mainImageUrl,data.id,data.year,data.price,"
    "pagination.hasMore,pagination.nextCursor,pagination.total,"
    "data.brand.name,data.brand.alias,data.model.alias,data.model.name,"
    "data.locations.city,data.locations.state,data.locations.geoPoint.lon,data.locations.geoPoint.lat,"
    "data.exteriorColor.name,data.interiorColor.name"
)

# Listing detail page — ``/car/{brand-alias}/{model-alias}/{year}/{vin}/{car-id}``.
# All pieces come from the GraphQL list response so no extra lookup needed.
DETAIL_URL_TEMPLATE = "{origin}/car/{brand_alias}/{model_alias}/{year}/{vin}/{car_id}"

# Canadian provinces — used to tag country as Canada vs USA since DuPont lists
# dealers from both countries in the same feed.
CA_PROVINCES = frozenset({
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
    "ON", "PE", "QC", "SK", "YT",
})
