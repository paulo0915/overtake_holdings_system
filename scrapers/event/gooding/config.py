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
