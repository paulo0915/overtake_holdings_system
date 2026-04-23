"""JamesEdition — HTML-only scrape (Cloudflare-fronted Rails app).

No JSON API. The list HTML carries ``ListingCard`` divs with rich
``data-*`` attributes (id, USD-converted price, source price/currency,
brand, model, country, city). The detail HTML adds VIN, engine/gearbox,
colours, mileage (mi+km), and dealer-written "About This Car" prose.

Cloudflare rate-limits aggressive request rates, so concurrency is low
and we throttle between fetches.
"""

SITE_ORIGIN = "https://www.jamesedition.com"
LIST_PATH = "/cars/all"

# 30 tiles per page (JE's default — we accept whatever the SRP returns;
# changing ``per_page`` isn't supported on this endpoint).
DEFAULT_PAGE_SIZE = 30

# Hard cap — JE shows "results exceeded" after ~400 pages. The luxury pool
# under a reasonable ``--min-price`` stays well inside that.
HARD_PAGE_CAP = 400

# Sort param is ``order`` (not ``sort``); the dropdown value for
# highest-first is ``price_desc``.
SORT_PARAM_NAME = "order"
SORT_PRICE_DESC = "price_desc"

# Detail-page URLs look like ``/cars/<brand-slug>/<model-slug>/<slug>-for-sale-<id>``.
# We store the full URL as ``Listing_url`` and parse the listing id off the end
# for dedup.
