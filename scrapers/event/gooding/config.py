"""Gooding & Company — Gatsby static site with per-lot ``page-data.json``."""

SITE_ORIGIN = "https://www.goodingco.com"

# The homepage loads an ``app-*.js`` bundle whose hash changes each deploy.
# We scrape the current hash from the homepage HTML then fetch the bundle and
# extract the embedded ``[{path, matchPath}, …]`` list (contains all lot URLs).
APP_JS_HREF_RE = r"/app-[a-f0-9]+\.js"

# Gatsby per-page payload. For a lot at ``/lot/<slug>``, the JSON lives at
# ``/page-data/lot/<slug>/page-data.json``.
PAGE_DATA_PATH_TEMPLATE = "/page-data{path}/page-data.json"

# Cloudinary CDN host for lot images.
IMAGE_CDN_BASE = "https://media.goodingco.com/image/upload/v1"

# --- Upcoming-mode GraphQL ---------------------------------------------------
# The early-consignment vehicles for an upcoming auction are NOT statically
# built into the Gatsby bundle; the lots page fetches them at runtime from the
# Apollo persisted-query endpoint below. We replay the same call to discover
# slugs, then read each lot's page-data the normal way.
GRAPHQL_URL = "https://cdn.goodingco.com/graphql"
GRAPHQL_OPERATION = "GetVehiclesAndFilters"
# Persisted query hash from the live site (Apollo Persisted Queries v1).
# If the site rebuilds the schema the hash will change and the call will 404
# — re-capture it from a browser request when that happens.
GRAPHQL_PERSISTED_HASH = (
    "0749eee74b0c4cf20bde3cd2a79b36ca364b6d1e4364ce3295a1ae4dd090706a"
)
