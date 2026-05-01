"""AutoScout24 — marketplace list / detail URLs."""

SITE_ORIGIN = "https://www.autoscout24.com"

# Default: major EU markets, cars only, undamaged, sort price descending (expensive first).
# ``ustate``: U=used, N=new (comma-encoded as N%2CU for both).
LIST_QUERY_BASE = (
    "atype=C"
    "&cy=D%2CA%2CB%2CE%2CF%2CI%2CL%2CNL"  # DE, AT, BE, ES, FR, IT, LU, NL
    "&damaged_listing=exclude"
    "&desc=1"
    "&powertype=kw"
    "&sort=price"
)

MODE_USED = "used"
MODE_NEW = "new"
MODE_ALL = "all"

# Site returns ~17–20 listings per JSON page; ``numberOfPages`` is capped (often 200).
HARD_PAGE_CAP = 200
