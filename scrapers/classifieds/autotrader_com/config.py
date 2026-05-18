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

# AutoTrader's price-DESC sort caps deep pagination at ~300-500 records per
# query, even when ``searchRadius=0`` ("national"). To enumerate the full
# $150k+ inventory we run separate queries seeded from different metros,
# each with a finite radius so the API treats them as independent searches.
# Each shard contributes its top ~300 high-end listings; dedup by URL across
# shards gives the union (~3,000-6,000 unique listings expected).
#
# Zip + radius pairs cover US high-end car density: Bay Area, LA/OC, Seattle,
# Las Vegas/Phoenix/Denver/Salt Lake, TX trio, FL trio, GA, Midwest,
# Northeast (NYC/Greenwich/Boston/DC/Philly), plus Charlotte/Nashville/Atlanta.
# Radius 250mi gives ~80% US coverage with sensible overlap; the URL-dedup
# step trims the overlap.
DEFAULT_SHARD_RADIUS_MI = 100
# ~70 zip shards: every US state has at least one seed metro, with dense
# coverage in high-end car markets (CA, FL, TX, NY, NJ, FL, mountain
# luxury enclaves). 250mi radius produces overlap between neighboring
# shards — the URL-dedup + "stop after 2 all-dupe pages" logic handles
# this cheaply (proven in smoke test: Greenwich/Philly/Ashburn dropped
# out in 2-3 pages when their results were already in NYC/DC shards).
# Heavy oversampling beats coverage gaps — a missed zip in WY means
# every $150k+ listing in a 250mi radius is lost; a duplicate zip in NJ
# costs ~3-5 pages of wasted work.
ZIP_SHARDS: list[tuple[str, str, str]] = [
    # --- California (dense) ---
    ("90210", "Beverly Hills", "CA"),
    ("92660", "Newport Beach", "CA"),
    ("92037", "La Jolla", "CA"),
    ("92101", "San Diego", "CA"),
    ("94102", "San Francisco", "CA"),
    ("94301", "Palo Alto", "CA"),
    ("93921", "Carmel", "CA"),
    ("95814", "Sacramento", "CA"),
    ("93701", "Fresno", "CA"),
    # --- Pacific Northwest ---
    ("98101", "Seattle", "WA"),
    ("99201", "Spokane", "WA"),
    ("97201", "Portland", "OR"),
    # --- Mountain West / SW ---
    ("89109", "Las Vegas", "NV"),
    ("85003", "Phoenix", "AZ"),
    ("85258", "Scottsdale", "AZ"),
    ("85701", "Tucson", "AZ"),
    ("87102", "Albuquerque", "NM"),
    ("80202", "Denver", "CO"),
    ("81611", "Aspen", "CO"),
    ("81657", "Vail", "CO"),
    ("84101", "Salt Lake City", "UT"),
    ("84060", "Park City", "UT"),
    ("83702", "Boise", "ID"),
    ("83001", "Jackson", "WY"),
    ("82001", "Cheyenne", "WY"),
    ("59101", "Billings", "MT"),
    # --- Plains ---
    ("68102", "Omaha", "NE"),
    ("50309", "Des Moines", "IA"),
    ("67202", "Wichita", "KS"),
    ("73102", "Oklahoma City", "OK"),
    ("64108", "Kansas City", "MO"),
    ("58102", "Fargo", "ND"),
    ("57104", "Sioux Falls", "SD"),
    # --- Texas (dense) ---
    ("75201", "Dallas", "TX"),
    ("77002", "Houston", "TX"),
    ("78701", "Austin", "TX"),
    ("78205", "San Antonio", "TX"),
    ("79901", "El Paso", "TX"),
    # --- South ---
    ("70112", "New Orleans", "LA"),
    ("39201", "Jackson", "MS"),
    ("38103", "Memphis", "TN"),
    ("35203", "Birmingham", "AL"),
    ("72201", "Little Rock", "AR"),
    # --- Florida (dense) ---
    ("33131", "Miami", "FL"),
    ("33480", "Palm Beach", "FL"),
    ("34102", "Naples", "FL"),
    ("33602", "Tampa", "FL"),
    ("32801", "Orlando", "FL"),
    ("32202", "Jacksonville", "FL"),
    # --- Southeast ---
    ("30309", "Atlanta", "GA"),
    ("29928", "Hilton Head", "SC"),
    ("29401", "Charleston", "SC"),
    ("28202", "Charlotte", "NC"),
    ("27601", "Raleigh", "NC"),
    ("37203", "Nashville", "TN"),
    ("40202", "Louisville", "KY"),
    # --- Midwest ---
    ("60601", "Chicago", "IL"),
    ("48226", "Detroit", "MI"),
    ("49503", "Grand Rapids", "MI"),
    ("44102", "Cleveland", "OH"),
    ("45202", "Cincinnati", "OH"),
    ("43215", "Columbus", "OH"),
    ("46204", "Indianapolis", "IN"),
    ("63101", "St. Louis", "MO"),
    ("53202", "Milwaukee", "WI"),
    ("55403", "Minneapolis", "MN"),
    # --- Mid-Atlantic ---
    ("20001", "Washington", "DC"),
    ("20146", "Ashburn", "VA"),  # legacy default; kept for continuity
    ("23219", "Richmond", "VA"),
    ("21201", "Baltimore", "MD"),
    ("19103", "Philadelphia", "PA"),
    ("15222", "Pittsburgh", "PA"),
    ("25301", "Charleston", "WV"),
    # --- Northeast ---
    ("10001", "New York", "NY"),
    ("11932", "Bridgehampton", "NY"),
    ("12207", "Albany", "NY"),
    ("14202", "Buffalo", "NY"),
    ("06830", "Greenwich", "CT"),
    ("06103", "Hartford", "CT"),
    ("02108", "Boston", "MA"),
    ("02903", "Providence", "RI"),
    ("03101", "Manchester", "NH"),
    ("05401", "Burlington", "VT"),
    ("04101", "Portland", "ME"),
    ("08540", "Princeton", "NJ"),
    ("19801", "Wilmington", "DE"),
    # --- Pacific outliers (thin $150k+ inventory; dedup skips fast) ---
    ("96813", "Honolulu", "HI"),
    ("99501", "Anchorage", "AK"),
    # === Secondary metros / regional dealer hubs (added when we dropped to
    # 100mi radius — denser coverage needed so no part of the country sits
    # > 100mi from a seed zip). ===
    # --- California gaps ---
    ("90401", "Santa Monica", "CA"),
    ("91101", "Pasadena", "CA"),
    ("90802", "Long Beach", "CA"),
    ("92805", "Anaheim", "CA"),
    ("95110", "San Jose", "CA"),
    ("94612", "Oakland", "CA"),
    ("93101", "Santa Barbara", "CA"),
    ("93301", "Bakersfield", "CA"),
    # --- Florida gaps ---
    ("33301", "Fort Lauderdale", "FL"),
    ("33432", "Boca Raton", "FL"),
    ("33901", "Fort Myers", "FL"),
    ("32501", "Pensacola", "FL"),
    ("32301", "Tallahassee", "FL"),
    # --- Texas gaps ---
    ("76102", "Fort Worth", "TX"),
    ("75024", "Plano", "TX"),
    ("78401", "Corpus Christi", "TX"),
    ("78501", "McAllen", "TX"),
    ("79401", "Lubbock", "TX"),
    ("79101", "Amarillo", "TX"),
    # --- New York / NJ gaps ---
    ("10601", "White Plains", "NY"),
    ("13202", "Syracuse", "NY"),
    ("14604", "Rochester", "NY"),
    ("11968", "Southampton", "NY"),
    ("07102", "Newark", "NJ"),
    ("08401", "Atlantic City", "NJ"),
    ("08002", "Cherry Hill", "NJ"),
    # --- PA / VA gaps ---
    ("17101", "Harrisburg", "PA"),
    ("18101", "Allentown", "PA"),
    ("23501", "Norfolk", "VA"),
    ("24011", "Roanoke", "VA"),
    # --- Carolinas / Georgia gaps ---
    ("28801", "Asheville", "NC"),
    ("27401", "Greensboro", "NC"),
    ("31401", "Savannah", "GA"),
    ("30901", "Augusta", "GA"),
    ("31201", "Macon", "GA"),
    # --- Midwest gaps ---
    ("48104", "Ann Arbor", "MI"),
    ("48933", "Lansing", "MI"),
    ("49684", "Traverse City", "MI"),
    ("43604", "Toledo", "OH"),
    ("45402", "Dayton", "OH"),
    ("46802", "Fort Wayne", "IN"),
    ("47408", "Bloomington", "IN"),
    ("62701", "Springfield", "IL"),
    ("61602", "Peoria", "IL"),
    ("61101", "Rockford", "IL"),
    ("53703", "Madison", "WI"),
    ("54301", "Green Bay", "WI"),
    ("55802", "Duluth", "MN"),
    ("55901", "Rochester", "MN"),
    # --- Mountain / SW gaps ---
    ("80903", "Colorado Springs", "CO"),
    ("85281", "Tempe", "AZ"),
    ("86001", "Flagstaff", "AZ"),
    ("89501", "Reno", "NV"),
    ("84601", "Provo", "UT"),
    ("59715", "Bozeman", "MT"),
    ("59801", "Missoula", "MT"),
    ("83401", "Idaho Falls", "ID"),
    # --- Pacific NW gaps ---
    ("97401", "Eugene", "OR"),
    ("97701", "Bend", "OR"),
    ("97501", "Medford", "OR"),
    ("98402", "Tacoma", "WA"),
    ("98004", "Bellevue", "WA"),
    ("98660", "Vancouver", "WA"),
    # --- Plains gaps ---
    ("68508", "Lincoln", "NE"),
    ("66603", "Topeka", "KS"),
    ("65801", "Springfield", "MO"),
    ("65201", "Columbia", "MO"),
    ("74103", "Tulsa", "OK"),
    ("72701", "Fayetteville", "AR"),
    ("58501", "Bismarck", "ND"),
    ("57701", "Rapid City", "SD"),
    # --- South gaps ---
    ("37902", "Knoxville", "TN"),
    ("40507", "Lexington", "KY"),
    ("26505", "Morgantown", "WV"),
    ("25701", "Huntington", "WV"),
    ("36601", "Mobile", "AL"),
    ("35801", "Huntsville", "AL"),
    ("36104", "Montgomery", "AL"),
    ("39501", "Gulfport", "MS"),
    ("39401", "Hattiesburg", "MS"),
    ("70801", "Baton Rouge", "LA"),
    ("71101", "Shreveport", "LA"),
    # --- New England gaps ---
    ("04401", "Bangor", "ME"),
    ("03301", "Concord", "NH"),
    ("05602", "Montpelier", "VT"),
    ("19711", "Newark", "DE"),
]

# --- Make-based sharding ---
# AutoTrader's REST endpoint accepts ``makeCodeList=<CODE>`` to scope a
# search to one manufacturer. Each per-make query gets its own ~300-500
# depth allowance from Akamai's anti-enumeration logic, so iterating over
# the high-end brands surfaces listings that geo-only sharding misses
# (e.g., a Bentley dealer in Boise that no metro shard reaches).
#
# Codes are AutoTrader's internal slugs (visible in their SRP URLs as
# ``?makeCodeList=FERR``). If a code returns 0 results in the log it
# probably means the slug changed — update from the live URL.
LUXURY_MAKE_CODES: list[tuple[str, str]] = [
    # (display_name, makeCodeList value)
    ("Ferrari",       "FERR"),
    ("Lamborghini",   "LAM"),
    ("McLaren",       "MCLAREN"),
    ("Bugatti",       "BUG"),
    ("Pagani",        "PAG"),
    ("Koenigsegg",    "KOEN"),
    ("Bentley",       "BENT"),
    ("Rolls-Royce",   "ROL"),
    ("Aston Martin",  "AM"),
    ("Maserati",      "MAS"),
    ("Porsche",       "POR"),
    ("Mercedes-Benz", "MB"),
    ("BMW",           "BMW"),
    ("Audi",          "AUDI"),
    ("Lotus",         "LOTUS"),
    ("Alfa Romeo",    "ALFA"),
    ("Lexus",         "LEXUS"),  # LFA, LC500 collectors
    ("Genesis",       "GENESIS"),  # G90 high-end
    ("Cadillac",      "CAD"),  # Celestiq, Escalade-V
    ("Lincoln",       "LIN"),  # Aviator BlackLabel
    ("Acura",         "ACURA"),  # NSX
    ("Ford",          "FORD"),  # GT, Shelby GT500
    ("Chevrolet",     "CHEV"),  # Corvette Z06/ZR1
    ("Dodge",         "DODGE"),  # Viper, Demon, Hellcat Redeye
    ("Tesla",         "TESLA"),  # Roadster, Plaid
]

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
