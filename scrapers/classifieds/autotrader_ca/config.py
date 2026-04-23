"""AutoTrader.ca — AutoScout24-powered Next.js SRP.

The site is a Next.js app that hydrates listings via ``_next/data/<build>/``
JSON endpoints. Each SRP mode (``ot_new`` / ``ot_used``) hits the same data
loader with different query params. The build id rotates on deploy, so we
discover it dynamically by fetching the HTML SRP once and reading
``window.__NEXT_DATA__.buildId``.

Descriptions (dealer-written prose) ship in the list response — no detail
fetch needed. Prices are CAD. Mileage is in kilometres.
"""

SITE_ORIGIN = "https://www.autotrader.ca"

# SRP paths feed the Next.js route ``/legacyTrader/[...slug]``. Each mode
# has its own HTML SRP URL and a matching JSON data URL keyed by buildId.
MODE_USED = "used"
MODE_NEW = "new"

# Slug that appears in both the HTML path and the JSON path.
_SLUG_FOR_MODE = {
    MODE_USED: "ot_used",
    MODE_NEW: "ot_new",
}


def slug_for_mode(mode: str) -> str:
    """Map ``used``/``new`` → URL slug (``ot_used`` / ``ot_new``)."""
    return _SLUG_FOR_MODE.get(mode, _SLUG_FOR_MODE[MODE_USED])


# 20 listings per page matches the browser's default (``size=20``).
DEFAULT_PAGE_SIZE = 20

# AutoTrader.ca caps pagination at 200 pages (``numberOfPages: 200``) regardless
# of the ``numberOfResults`` (188k+) the facet count reports. With a high
# ``--min-price`` the luxury pool stays well inside that.
HARD_PAGE_CAP = 200

# Canada-specific. Prices come back in CAD; the FX helper converts to USD.
DEFAULT_CURRENCY = "CAD"

# ``cy=CA`` restricts to Canadian inventory. ``damaged_listing=exclude`` hides
# salvage/damaged cars we don't want. ``ustate`` + ``offer`` differentiate the
# two modes — new cars carry ``offer=N``, used carry ``offer=U``.
BASE_QUERY_COMMON = {
    "cy": "CA",
    "damaged_listing": "exclude",
    "desc": "1",
    "sort": "price",
    "ustate": "N,U",
    "atype": "C",
}


def build_mode_query(mode: str) -> dict[str, str]:
    """Per-mode extras on top of ``BASE_QUERY_COMMON``."""
    if mode == MODE_NEW:
        return {**BASE_QUERY_COMMON, "offer": "N"}
    return {**BASE_QUERY_COMMON, "offer": "U"}
