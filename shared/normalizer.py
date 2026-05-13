"""Shared data normalization — mileage, transmission, engine, dates, country."""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone

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
    "monaco": "Monaco", "monte carlo": "Monaco", "monte-carlo": "Monaco",
    "cernobbio": "Italy", "villa d'este": "Italy",
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
    # Monaco is a sovereign state, not France. Typesense / geocoding services
    # sometimes return "Monte Carlo" or "France" for Monaco-based events.
    "monte carlo": "Monaco",
    "monte-carlo": "Monaco",
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
    """Normalize a VIN or chassis number string.

    Standard 17-char VINs (ISO 3779) are uppercased and returned as-is.
    All other chassis/serial numbers (pre-1981, race cars, short serials like
    ``418``) are returned with only truly invalid characters removed — case is
    preserved and no minimum length is enforced.

    Returns ``None`` only for empty strings, known placeholders, or strings
    longer than 25 chars after cleaning.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in _VIN_PLACEHOLDERS:
        return None

    # Bonhams-style ``<vin>ENGINENO<engineno>`` concat — strip on uppercase match.
    if "ENGINENO" in s.upper():
        s = s[:s.upper().index("ENGINENO")].strip()
        if not s:
            return None

    # Standard 17-char VIN: uppercase and validate.
    s_up = re.sub(r"\s+", "", s).upper()
    if _VIN_RE.match(s_up):
        # WMI never starts with '0' in real VINs — zero-prefixed = dealer inventory ID.
        if s_up[0] == "0":
            return None
        return s_up

    # Partially-masked 17-char VINs (e.g. "WP0ZZZ99ZSS******").
    if len(s_up) == 17 and "*" in s_up and re.match(r"^[A-HJ-NPR-Z0-9*]{17}$", s_up):
        return s_up

    # All other chassis/serial numbers: uppercase, strip truly strange chars.
    # Keep letters, digits, hyphen, dot, slash.
    cleaned = re.sub(r"[^a-zA-Z0-9\-./]", "", s).strip("-./").upper()
    if not cleaned or cleaned in _VIN_PLACEHOLDERS:
        return None
    if len(cleaned) > 25:
        return None
    return cleaned


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


# Matches any HTML/XML tag, including self-closing ones like <br/>, <hr>, etc.
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
# Collapse runs of whitespace (spaces, tabs, non-breaking spaces) to a single space,
# but preserve intentional newlines so paragraph structure survives.
_INLINE_WS_RE = re.compile(r"[ \t ]+")
# Collapse 3+ consecutive newlines to at most 2.
_EXCESS_NL_RE = re.compile(r"\n{3,}")


def strip_html(text: str | None) -> str | None:
    """Remove all HTML tags from *text* and normalise whitespace.

    Converts ``<br>``, ``<p>``, ``<li>`` etc. to newlines before stripping so
    that paragraph / list structure is preserved as readable plain text rather
    than running every sentence together.
    """
    if not text:
        return text
    s = str(text)
    # Skip entirely if the string contains no tag-like content.
    if "<" not in s:
        return s
    # Turn block/line-break tags into newlines before stripping everything else.
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(?:p|div|li|tr|td|th|h[1-6]|blockquote|hr|section|article)[^>]*>",
               "\n", s, flags=re.I)
    s = _HTML_TAG_RE.sub("", s)
    s = html_lib.unescape(s)
    # Normalise inline whitespace, then tidy up newlines.
    s = _INLINE_WS_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    s = _EXCESS_NL_RE.sub("\n\n", s)
    return s.strip() or None


_YEAR_RE = re.compile(
    r'\b(1[89]\d{2}|20\d{2})(?![\d,]*\s*k?\s*-\s*(?:mile|km|kilometer))\b',
    re.IGNORECASE,
)


def parse_year_from_title(title: str) -> int | None:
    """Extract year from listing title, ignoring mileage patterns."""
    match = _YEAR_RE.search(title)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Makes that are definitively NOT passenger cars.  Any scraped record whose
# Make field (after normalisation) exactly matches one of these values is
# rejected by the vehicle filter regardless of the whitelist spreadsheet.
# ---------------------------------------------------------------------------
NON_CAR_MAKES: frozenset[str] = frozenset({
    # ── Aircraft ──────────────────────────────────────────────────────────
    "Bombardier", "Learjet", "Lear", "Cessna", "Gulfstream", "Piper",
    "Beechcraft", "Dassault", "Embraer", "Airbus", "Boeing",
    "De Havilland",   # aircraft OEM (not the car)
    # ── Boats / watercraft ─────────────────────────────────────────────────
    "Riva", "Chris-Craft", "Midnight Express", "Sea Ray", "Bertram",
    "Hatteras", "Sunseeker", "Azimut", "Ferretti", "Pershing",
    "Princess", "Beneteau", "Catalina", "Malibu",
    "Cigarette", "Donzi", "Fountain", "Cobalt", "Chaparral",
    "Wellcraft", "Regal", "Four Winns", "Chaparral",
    "Grand Banks", "Carver", "Viking", "Cabo", "Luhrs",
    # ── Commercial trucks ──────────────────────────────────────────────────
    "Kenworth", "Peterbilt", "Mack", "Western Star", "Freightliner",
    # Note: "International" alone is ambiguous (pre-war cars exist), so we
    # block "International Harvester" and rely on model checks for trucks.
    "International Harvester",
    # ── Buses / coaches ────────────────────────────────────────────────────
    "Bedford",          # Bedford trucks/buses (not the historic Bedford car)
    "Blue Bird",
    "Thomas Built",
    # ── Scooters / mopeds / go-karts / ATVs ───────────────────────────────
    "Piaggio",          # Vespa/Piaggio scooters — not cars
    "Vespa",
    "Roketa",           # go-kart / ATV maker
    "Kymco",
    "TaoTao",
})

# Lowercase set for fast case-insensitive lookup.
_NON_CAR_MAKES_LOWER: frozenset[str] = frozenset(m.lower() for m in NON_CAR_MAKES)


def is_non_car_make(make: str | None) -> bool:
    """Return True when *make* is a definitively non-car manufacturer."""
    if not make:
        return False
    return make.strip().lower() in _NON_CAR_MAKES_LOWER


_KNOWN_MAKES = [
    "Aston Martin", "Gordon Murray Automotive", "Mercedes-Benz", "Mercedes-AMG",
    "Alfa Romeo", "Land Rover", "De Tomaso", "Rolls-Royce",
    "Ferrari", "Lamborghini", "Porsche", "Bugatti", "McLaren", "Pagani",
    "Koenigsegg", "BMW", "Audi", "Ford", "Honda", "Acura", "Lexus",
    "Nissan", "Toyota", "Chevrolet", "Dodge", "Shelby", "Lancia",
    "Maserati", "Bentley", "Jaguar", "Lotus", "Fiat",
    "Pontiac", "Plymouth", "AMC", "Saleen", "SSC", "Rimac",
    "Hennessey", "W Motors", "Czinger", "Lucid", "Tesla",
    "Bristol Cars", "Pininfarina",
]

# Sorted longest-first so multi-word makes match before their single-word prefix.
_KNOWN_MAKES_SORTED = sorted(_KNOWN_MAKES, key=lambda m: len(m), reverse=True)

# Lowercase set for fast case-insensitive membership checks.
_KNOWN_MAKES_LOWER: frozenset[str] = frozenset(m.lower() for m in _KNOWN_MAKES)

# Canonical casing corrections keyed by lowercase variant.
_MAKE_CASING = {
    "mclaren": "McLaren",
    "gmc": "GMC",
    "bmw": "BMW",
    "amg": "AMG",
    "tvr": "TVR",
    "mgb": "MGB",
    "mg": "MG",
    "bmw alpina": "BMW Alpina",
}

# Remap entries: (canonical_make, prepend_old_make_to_model).
# prepend=True  → old make string is a model-line, not a brand (Range Rover).
# prepend=False → old make is just a misspelling / missing hyphen (Rolls Royce).
_MAKE_REMAP: dict[str, tuple[str, bool]] = {
    "range rover": ("Land Rover", True),
    "rolls royce": ("Rolls-Royce", False),
    # "Mercedes" alone is the truncated brand; ensure -Benz is always appended.
    "mercedes": ("Mercedes-Benz", False),
    # "Mercedes Benz" (space, no hyphen) — normalise to canonical form.
    "mercedes benz": ("Mercedes-Benz", False),
    # Revology builds continuation Mustangs — treat as Ford.
    "revology": ("Ford", False),
}


def normalize_make_model(
    make: str | None,
    model: str | None,
    *,
    title: str | None = None,
) -> tuple[str | None, str | None]:
    """Fix multi-word make splits and canonical casing.

    When a source returns Make="Aston" / Model="Martin DB4 …" (first-space
    split bug), reconstruct from make+model and re-match against the known
    multi-word makes list.  Also applies casing corrections (McLaren, GMC…)
    and remaps pseudo-makes like "Range Rover" → "Land Rover".

    Arguments:
        make:  raw Make value from the source scraper.
        model: raw Model value from the source scraper.
        title: full listing title (optional) — used when make/model are both
               None/empty to attempt title-based extraction as a last resort.
    """
    make_s = (make or "").strip()
    model_s = (model or "").strip()

    # ── Remap pseudo-makes / casing corrections ───────────────────────────────
    remap_entry = _MAKE_REMAP.get(make_s.lower())
    if remap_entry:
        canonical, prepend = remap_entry
        if prepend:
            # "Range Rover" is a model line → prepend it to the model string
            model_s = (f"{make_s} {model_s}").strip() if model_s else make_s
        make_s = canonical
        # When source splits "Mercedes-Benz 300SL" as Make="Mercedes"/Model="Benz 300SL"
        # the model will now incorrectly start with "Benz". Strip it.
        if make_s == "Mercedes-Benz" and model_s.lower().startswith("benz "):
            model_s = model_s[5:].strip()

    # ── Longest-match re-split for multi-word makes ───────────────────────────
    combined = f"{make_s} {model_s}".strip() if model_s else make_s
    for known in _KNOWN_MAKES_SORTED:
        if combined.lower().startswith(known.lower()):
            remainder = combined[len(known):].strip()
            make_s = known
            model_s = remainder
            break

    # ── Casing corrections ────────────────────────────────────────────────────
    cased = _MAKE_CASING.get(make_s.lower())
    if cased:
        make_s = cased

    # ── Null-out placeholder strings ──────────────────────────────────────────
    _PLACEHOLDER = frozenset({"unspecified", "unknown", "other", "n/a", "none", ""})
    if make_s.lower() in _PLACEHOLDER:
        make_s = ""
    if model_s.lower() in _PLACEHOLDER:
        model_s = ""

    # ── Title fallback when make is empty or not a recognised make ───────────
    is_unknown_make = make_s and make_s.lower() not in _KNOWN_MAKES_LOWER and make_s.lower() not in _NON_CAR_MAKES_LOWER
    if (not make_s or is_unknown_make) and title:
        inferred_make, inferred_model = parse_make_model_from_title(title)
        if inferred_make and inferred_make.lower() in _KNOWN_MAKES_LOWER:
            make_s = inferred_make
            if not model_s:
                model_s = inferred_model

    return (make_s or None, model_s or None)


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

    for make in _KNOWN_MAKES_SORTED:
        if after_year.lower().startswith(make.lower()):
            model = after_year[len(make):].strip()
            return (make, model)

    # If no known make starts the string, scan further into the title.
    # Handles titles like "c. 1967 Automobile S.C.A.F. Ferrari 330 P2"
    # where the year-stripped prefix is not the make.
    after_lower = after_year.lower()
    for make in _KNOWN_MAKES_SORTED:
        idx = after_lower.find(make.lower())
        if idx > 0:
            model = after_year[idx + len(make):].strip()
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


def get_scraped_at() -> str:
    """Return current UTC timestamp in ISO 8601 format (Z suffix).

    Brief spec: Scraped_at must be UTC ending with 'Z' (e.g. 2026-04-22T23:29:05Z).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc_z(ts: str | None) -> str | None:
    """Normalise an ISO 8601 timestamp (with any offset) to UTC ending with 'Z'.

    Returns *None* when the input is falsy or unparseable.

    Examples:
        "2026-04-22T16:25:01-0700" → "2026-04-22T23:25:01Z"
        "2026-04-22T23:25:01Z"     → "2026-04-22T23:25:01Z"   (no-op)
        "2026-04-22T23:25:01+00:00"→ "2026-04-22T23:25:01Z"
    """
    if not ts:
        return None
    ts_s = str(ts).strip()
    if not ts_s:
        return None
    # Already UTC-Z form
    if ts_s.endswith("Z"):
        return ts_s
    # Try standard fromisoformat (Python 3.11+) then fallback strptime variants
    for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(ts_s)
            else:
                dt = datetime.strptime(ts_s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            continue
    return ts_s  # return original if unparseable


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
    # Pattern A: lookahead stops at prepositions/descriptors — allow up to 2
    # extra words so "Striking Dark Green" / "Pearl Metallic White" are captured.
    re.compile(
        r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,2})'
        r'(?=\s+(?:' + _COLOR_STOP_WORDS + r'))',
        re.IGNORECASE,
    ),
    # Pattern B: no lookahead fallback — at most 2 additional words.
    re.compile(
        r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,2})',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:exterior\s+is|exterior\s+color\s*(?:is)?)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})',
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
