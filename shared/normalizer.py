"""Shared data normalization — mileage, transmission, engine, dates, country."""

import html as html_lib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pycountry

KM_TO_MILES = 0.621371

# Country code lookup cache
_COUNTRY_CACHE: dict[str, str] = {}

# City-to-country mapping (Section 5.7 of brief)
CITY_COUNTRY_MAP = {
    "monterey": "USA", "scottsdale": "USA", "amelia island": "USA",
    "las vegas": "USA", "indianapolis": "USA", "pebble beach": "USA",
    "greenwich": "USA", "hershey": "USA", "elkhart": "USA",
    "london": "UK", "newport pagnell": "UK", "goodwood": "UK",
    "paris": "France", "le mans": "France", "retromobile": "France",
    "geneva": "Switzerland", "zurich": "Switzerland",
    "munich": "Germany", "stuttgart": "Germany",
    "dubai": "UAE", "abu dhabi": "UAE",
    "monaco": "Monaco",
    "milan": "Italy", "maranello": "Italy",
    "tokyo": "Japan", "sydney": "Australia", "toronto": "Canada",
}


_COUNTRY_NAME_OVERRIDES = {
    "united states": "USA",
    "united states of america": "USA",
    "u.s.a.": "USA",
    "u.s.": "USA",
    "us": "USA",
    "usa": "USA",
    "united kingdom": "UK",
    "great britain": "UK",
    "england": "UK",
    "scotland": "UK",
    "wales": "UK",
    "northern ireland": "UK",
    "u.k.": "UK",
    "uk": "UK",
    "united arab emirates": "UAE",
    "u.a.e.": "UAE",
    "uae": "UAE",
}


def canonical_country_name(name: str | None) -> str | None:
    """Collapse synonyms (``United Kingdom`` ↔ ``UK``, ``United States`` ↔ ``USA``)."""
    if not name:
        return None
    s = str(name).strip()
    if not s:
        return None
    return _COUNTRY_NAME_OVERRIDES.get(s.lower(), s)


def country_code_to_name(code: str | None) -> str | None:
    """Convert ISO alpha-2 country code to canonical display name."""
    if not code:
        return None
    alpha2 = code.upper()
    if alpha2 in _COUNTRY_CACHE:
        return _COUNTRY_CACHE[alpha2]

    overrides = {"US": "USA", "GB": "UK", "AE": "UAE"}
    if alpha2 in overrides:
        _COUNTRY_CACHE[alpha2] = overrides[alpha2]
        return overrides[alpha2]

    country = pycountry.countries.get(alpha_2=alpha2)
    name = country.name if country else alpha2
    canon = canonical_country_name(name) or name
    _COUNTRY_CACHE[alpha2] = canon
    return canon


def city_to_country(city: str | None) -> str | None:
    """Map a city name to country using the lookup table."""
    if not city:
        return None
    return CITY_COUNTRY_MAP.get(city.strip().lower())


def normalize_mileage(raw: str | None, unit: str | None) -> int | None:
    """Convert raw mileage string to integer miles. Converts km to miles.

    Handles ``15k`` (→ 15,000), ``15,000 Km`` (→ unit-converted), and bare
    numbers. The ``k`` thousands-suffix is only applied when it appears
    *immediately* after the digits with no following letter, to avoid
    misreading the ``k`` in ``Km`` / ``km`` / ``kilometres`` as a multiplier.
    """
    if raw is None:
        return None

    raw = raw.strip().lower()
    # ``(?!m)`` — don't consume ``k`` when the next char starts a unit word
    # like ``km``, ``kilometre``, etc. (i.e. treat as unit, not a multiplier).
    match = re.search(r"([\d,]+\.?\d*)\s*(k)?(?![a-z])", raw)
    if not match:
        # Retry without the thousands-suffix filter in case the only number
        # in the string is followed by a unit word.
        match = re.search(r"([\d,]+\.?\d*)", raw)
        if not match:
            return None
        num_str = match.group(1).replace(",", "")
        has_k = False
    else:
        num_str = match.group(1).replace(",", "")
        has_k = match.group(2) is not None

    try:
        value = float(num_str)
    except ValueError:
        return None

    if has_k:
        value *= 1000
    # Auto-detect unit from the raw text if the caller didn't supply one.
    if unit is None:
        if "mile" in raw or re.search(r"\bmi\b", raw):
            unit = "mi"
        elif "km" in raw or "kilomet" in raw:
            unit = "km"
    if unit == "km":
        value *= KM_TO_MILES

    return round(value)


def normalize_engine_spec(raw: str | None) -> str | None:
    """Normalize engine spec to consistent 'X.X-Liter' format.

    Rejects obvious junk values that some scrapers pass through from source
    APIs: bare zero/empty, single-number cubic-inch codes (``427``, ``762``),
    and bare cylinder-config tokens (``V8``, ``V-8``, ``LT6``). These are not
    useful on their own — returning ``None`` keeps the column honest.
    """
    if raw is None:
        return None

    text = html_lib.unescape(str(raw)).strip()
    if not text or text == "0":
        return None

    # Reject "0 0", "0 / 0", etc. — Barrett-Jackson emits this when the
    # source's engine field has two zero sub-fields.
    if re.fullmatch(r"0[\s/.,-]+0", text):
        return None

    text = re.sub(r'(?<=\d)\s*L(?=\s)', '-Liter', text)
    text = re.sub(r'(?i)[- ]litre\b', '-Liter', text)
    text = re.sub(r'(?i)[- ]liter\b', '-Liter', text)
    text = re.sub(r'(\d)\s+Liter', r'\1-Liter', text)
    text = text.replace("--", "-")
    text = text.strip()

    # Bare cylinder-config token (``V8``, ``V-8``, ``LT6``, ``W07``) or a bare
    # cubic-inch / displacement number without context is not a real spec.
    if re.fullmatch(r"[VLW]-?\d{1,2}|LT\d+|W\d+", text, re.I):
        return None
    if re.fullmatch(r"\d{1,6}(?:\.\d+)?", text):
        return None
    # Bare hyphenated number range (``12-84``) — usually a year range that got
    # mis-extracted, never a useful engine spec.
    if re.fullmatch(r"\d{1,4}-\d{1,4}", text):
        return None

    return text


# Modern VIN spec (ISO 3779): 17 chars, alphanumeric, excluding the letters
# I, O, Q (which can be confused with 1 and 0). Pre-1981 vehicles used shorter
# chassis numbers; race cars and one-offs use manufacturer-specific tags
# (Shelby ``CSX3015``, Ferrari ``B73RS``, Porsche ``J386``). None of those
# are legitimate VINs — they're chassis numbers, and scrapers that store
# them under ``VIN`` pollute the column. Downstream consumers treat the
# ``VIN`` column as "the thing you can decode with a standard VIN decoder",
# so we reject anything not matching the modern spec and return ``None``.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Obvious placeholder strings that occasionally survive VIN extraction.
# Checked case-insensitively after trimming.
_VIN_PLACEHOLDERS = frozenset({
    "", "-", "--", "---", "N/A", "NA", "NONE", "NULL", "TBD", "UNKNOWN",
    "PENDING", "REQUEST", "ON REQUEST", "AVAILABLE UPON REQUEST",
})


def normalize_vin(raw: str | None) -> str | None:
    """Return a valid 17-char VIN or ``None``.

    Valid = matches ISO 3779 format (``[A-HJ-NPR-Z0-9]{17}`` — 17 chars,
    alphanumeric, excluding ``I``/``O``/``Q``).

    Everything else (placeholders ``.``, ``001``, ``213``; chassis numbers
    like ``CSX3015``, ``DK28``, ``J386``; concat bugs like
    ``<vin>ENGINENO<engineno>``) returns ``None``. This keeps the VIN
    column decodable by standard VIN tools — race-car / pre-1981 chassis
    tags belong in a separate ``Chassis_number`` column, not VIN.

    Side effects:
      * Strips surrounding whitespace.
      * Uppercases (VINs are case-insensitive but standard form is upper).
      * Trims an accidental ``ENGINENO<engine-no>`` suffix (Bonhams parser
        bug — defensive catch here so bad rows still produce a clean VIN).
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s or s in _VIN_PLACEHOLDERS:
        return None

    # Bonhams-style ``<vin>ENGINENO<engineno>`` concat — defensive strip.
    # The scraper fix in the Bonhams parser should prevent this upstream
    # but we also catch it here in case any rows already landed in the DB
    # or a new source has the same join bug.
    if "ENGINENO" in s:
        s = s.split("ENGINENO", 1)[0].strip()
        if not s:
            return None

    if _VIN_RE.match(s):
        return s
    return None


def normalize_transmission(raw: str | None) -> str | None:
    """Normalize transmission to ``"Manual"`` or ``"Automatic"`` (or ``None``).

    The DB stores one of exactly these two title-cased labels; anything else
    (``"automatic (DSG)"``, ``"6-speed manual"``, etc.) collapses to one of
    them. If the raw string matches neither bucket, return ``None`` rather
    than forcing an invalid value.
    """
    if raw is None:
        return None

    raw_lower = raw.lower()

    auto_keywords = [
        "automatic", "auto", "dct", "pdk", "dual-clutch", "dual clutch",
        "sequential", "tiptronic", "s-tronic", "smg", "f1", "e-gear",
        "isr", "robotized", "robotised", "cvt", "torque converter",
        "semi-auto", "semi auto", "paddle",
        "speedshift", "mct", "sportshift", "sportomatic", "steptronic",
    ]
    manual_keywords = ["manual", "stick", "h-pattern"]

    if re.search(r'\d[- ]speed\s+manual', raw_lower):
        return "Manual"
    if re.search(r'\d[- ]speed\s+(?:' + '|'.join(auto_keywords) + ')', raw_lower):
        return "Automatic"

    for kw in auto_keywords:
        if kw in raw_lower:
            return "Automatic"
    for kw in manual_keywords:
        if kw in raw_lower:
            return "Manual"

    return None


def unescape(text: str | None) -> str | None:
    """Decode HTML entities like &#038; -> &."""
    if text is None:
        return None
    return html_lib.unescape(text)


_YEAR_RE = re.compile(
    r'\b(1[89]\d{2}|20\d{2})(?![\d,]*\s*k?\s*-\s*(?:mile|km|kilometer))\b',
    re.IGNORECASE,
)


def parse_year_from_title(title: str) -> int | None:
    """Extract year from listing title, ignoring mileage patterns."""
    match = _YEAR_RE.search(title)
    return int(match.group(1)) if match else None


_KNOWN_MAKES = [
    "Aston Martin", "Gordon Murray Automotive", "Mercedes-Benz", "Mercedes-AMG",
    "Alfa Romeo", "Land Rover", "De Tomaso",
    "Ferrari", "Lamborghini", "Porsche", "Bugatti", "McLaren", "Pagani",
    "Koenigsegg", "BMW", "Audi", "Ford", "Honda", "Acura", "Lexus",
    "Nissan", "Toyota", "Chevrolet", "Dodge", "Shelby", "Lancia",
    "Maserati", "Bentley", "Rolls-Royce", "Jaguar", "Lotus",
    "Pontiac", "Plymouth", "AMC", "Saleen", "SSC", "Rimac",
    "Hennessey", "W Motors", "Czinger", "Lucid", "Tesla",
]


def parse_make_model_from_title(title: str) -> tuple[str, str]:
    """Extract make and model from a listing title (fallback when page data unavailable)."""
    match = re.search(
        r'\b(1[89]\d{2}|20\d{2})(?![\d,]*\s*k?\s*-\s*(?:mile|km|kilometer))\b\s*',
        title, re.IGNORECASE,
    )
    if match:
        after_year = title[match.end():].strip()
    else:
        after_year = re.sub(
            r'^(?:[\d,]+k?-?(?:mile|km)\s+)?(?:no[- ]reserve\s+)?(?:euro\s+)?',
            '', title, flags=re.IGNORECASE,
        ).strip()

    for make in _KNOWN_MAKES:
        if after_year.lower().startswith(make.lower()):
            model = after_year[len(make):].strip()
            return (make, model)

    parts = after_year.split(None, 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    elif len(parts) == 1:
        return (parts[0], "")
    return ("", "")


def format_selling_date(timestamp: int | None, date_str: str | None) -> str | None:
    """Format selling date as YYYY-MM-DD."""
    if date_str:
        return date_str
    if timestamp:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    return None


_PT = ZoneInfo("America/Los_Angeles")


def get_scraped_at() -> str:
    """Return current Pacific Time timestamp in ISO 8601 format."""
    return datetime.now(_PT).strftime("%Y-%m-%dT%H:%M:%S%z")


# ── Description field extraction (shared by all scrapers) ───────────────────

# Words that appear before "interior / leather / upholstery" in descriptions
# but are NOT colors — used to reject false-positive color captures.
_NON_COLOR_WORDS = frozenset({
    "simplistic", "lightweight", "minimal", "minimalist", "basic", "standard",
    "custom", "factory", "original", "pure", "modern", "classic", "sport",
    "luxury", "premium", "exclusive", "functional", "race", "track",
    "stripped", "spartan", "purpose", "built", "open", "full", "partial",
    "optional",  # e.g. "with optional leather" in Hagerty / dealer copy
})

# Engine patterns — ordered by confidence/specificity.
# Pattern A handles the most common auction description style: displacement
# first, with induction qualifier optionally before OR after the number
# (e.g. "twin-turbocharged 3.8-liter flat-six" AND "3.8-liter twin-turbocharged flat-six").
_DESC_ENGINE_PATTERNS = [
    re.compile(
        r'((?:naturally[- ]aspirated|twin[- ]turbo(?:charged)?|quad[- ]turbo(?:charged)?|'
        r'turbocharged|supercharged|biturbo)\s+)?'
        r'([\d.]+[- ](?:liter|litre))'
        r'(?:\s+(?:naturally[- ]aspirated|twin[- ]turbo(?:charged)?|turbocharged|'
        r'supercharged|biturbo|DOHC|SOHC|OHV|AMG|hand[- ]built|dry[- ]sump)){0,3}'
        r'\s+(?:flat[- ]?\w+|V\d+|inline[- ]?\d+|straight[- ]?\d+|W\d+|I\d+|boxer|rotary)',
        re.IGNORECASE,
    ),
    # Pattern B: induction keyword + cylinder config, no displacement (fallback)
    re.compile(
        r'((?:biturbo|twin[- ]turbo(?:charged)?|turbocharged|supercharged|naturally[- ]aspirated)\s+'
        r'(?:V\d+|flat[- ]?\w+|inline[- ]?\d+|straight[- ]?\d+|W\d+|boxer|rotary))\s*(?:engine)?',
        re.IGNORECASE,
    ),
    # Pattern C: bare cylinder configuration as last resort (no displacement, no induction)
    re.compile(
        r'\b(flat[- ](?:six|eight|four|twelve|ten|two)|V\d+|W\d+|boxer[- ](?:six|four)|'
        r'inline[- ]?\d+|straight[- ]?\d+|rotary)\s*(?:engine|motor)?',
        re.IGNORECASE,
    ),
]

_DESC_TRANSMISSION_PATTERNS = [
    re.compile(
        r'(\w+[- ]speed\s+(?:manual|automatic|PDK|DCT|dual[- ]clutch|sequential|'
        r'tiptronic|S-tronic|SMG|F1|e-gear|ISR)(?:\s+(?:transmission|transaxle|gearbox))?)',
        re.IGNORECASE,
    ),
    re.compile(r'(\d+[- ]speed\s+(?:\w+\s+){1,4}(?:transmission|transaxle|gearbox))', re.IGNORECASE),
    re.compile(
        r'((?:manual|PDK|automatic|DCT|dual[- ]clutch|sequential|tiptronic)\s+'
        r'(?:transmission|transaxle|gearbox))',
        re.IGNORECASE,
    ),
    re.compile(r'\b(PDK|manual transmission|automatic transmission)\b', re.IGNORECASE),
]

# Stop-word set used in ext_color lookahead and int_color suffix guard.
_COLOR_STOP_WORDS = (
    r'over|with|and\b|accented|by\b|featuring|powered|carbon|,|\.|'
    r'leather|alcantara|suede|nappa|interior|upholstery|exterior'
)

_DESC_EXT_COLOR_PATTERNS = [
    # Pattern A: lookahead stops capture at prepositions/descriptors.
    # Uses {0,1} (not {0,2}) so greedy match cannot swallow a word that then
    # satisfies the lookahead via a word further downstream.
    re.compile(
        r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,1})'
        r'(?=\s+(?:' + _COLOR_STOP_WORDS + r'))',
        re.IGNORECASE,
    ),
    # Pattern B: no lookahead fallback — at most 1 additional word.
    re.compile(
        r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,1})',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:exterior\s+is|exterior\s+color\s*(?:is)?)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,1})',
        re.IGNORECASE,
    ),
]

_DESC_INT_COLOR_PATTERNS = [
    # Negative lookahead inside the optional suffix prevents the capture group
    # from consuming the terminal stop-word (e.g. "black leather" → "black").
    re.compile(
        r'over\s+(?:a\s+|an\s+)?'
        r'([A-Z][A-Za-z]+(?:\s+(?!leather|alcantara|suede|nappa|interior|upholstery)[A-Za-z]+)?)'
        r'\s+(?:leather|alcantara|suede|nappa|interior|upholstery)',
        re.IGNORECASE,
    ),
    re.compile(
        r'with\s+(?:a\s+|an\s+)?'
        r'([A-Z][A-Za-z]+(?:\s+(?!leather|alcantara|suede|nappa|interior|upholstery)[A-Za-z]+)?)'
        r'\s+(?:leather|alcantara|suede|nappa|interior|upholstery)',
        re.IGNORECASE,
    ),
    re.compile(r'([A-Z][A-Za-z]+)\s+leather\s+interior', re.IGNORECASE),
    # Stop at common prepositions/conjunctions that follow the color name.
    re.compile(
        r'(?:interior\s+is|interior\s+in)\s+'
        r'([A-Za-z]+(?:\s+(?!with\b|and\b|or\b|by\b|from\b)[A-Za-z]+){0,1})',
        re.IGNORECASE,
    ),
]


def extract_description_fields(text: str) -> dict[str, str | None]:
    """Extract engine, transmission, ext_color, int_color from free-form description text.

    Used as a shared fallback by all scrapers when structured API/HTML data is absent.
    Returns a dict with keys: engine, transmission, ext_color, int_color.
    Engine and transmission are returned already normalized.
    """
    if not text:
        return {"engine": None, "transmission": None, "ext_color": None, "int_color": None}

    engine = None
    for pat in _DESC_ENGINE_PATTERNS:
        m = pat.search(text)
        if m:
            engine = normalize_engine_spec(m.group(0).strip())
            break

    transmission = None
    for pat in _DESC_TRANSMISSION_PATTERNS:
        m = pat.search(text)
        if m:
            transmission = normalize_transmission(m.group(1))
            break

    ext_color = None
    for pat in _DESC_EXT_COLOR_PATTERNS:
        m = pat.search(text)
        if m:
            ext_color = m.group(1).strip()
            break

    int_color = None
    for pat in _DESC_INT_COLOR_PATTERNS:
        m = pat.search(text)
        if m:
            captured = m.group(1).strip()
            if not any(w in _NON_COLOR_WORDS for w in captured.lower().split()):
                int_color = captured
                break

    return {"engine": engine, "transmission": transmission, "ext_color": ext_color, "int_color": int_color}
