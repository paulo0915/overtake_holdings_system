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


def country_code_to_name(code: str | None) -> str | None:
    """Convert ISO alpha-2 country code to display name via pycountry."""
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
    _COUNTRY_CACHE[alpha2] = name
    return name


def city_to_country(city: str | None) -> str | None:
    """Map a city name to country using the lookup table."""
    if not city:
        return None
    return CITY_COUNTRY_MAP.get(city.strip().lower())


def normalize_mileage(raw: str | None, unit: str | None) -> int | None:
    """Convert raw mileage string to integer miles. Converts km to miles."""
    if raw is None:
        return None

    raw = raw.strip().lower()
    match = re.search(r'([\d,]+\.?\d*)\s*(k)?', raw)
    if not match:
        return None

    num_str = match.group(1).replace(",", "")
    has_k = match.group(2) is not None

    try:
        value = float(num_str)
    except ValueError:
        return None

    if has_k:
        value *= 1000
    if unit == "km":
        value *= KM_TO_MILES

    return round(value)


def normalize_engine_spec(raw: str | None) -> str | None:
    """Normalize engine spec to consistent 'X.X-Liter' format."""
    if raw is None:
        return None

    text = html_lib.unescape(raw).strip()
    if not text:
        return None

    text = re.sub(r'(?<=\d)\s*L(?=\s)', '-Liter', text)
    text = re.sub(r'(?i)[- ]litre\b', '-Liter', text)
    text = re.sub(r'(?i)[- ]liter\b', '-Liter', text)
    text = re.sub(r'(\d)\s+Liter', r'\1-Liter', text)
    text = text.replace("--", "-")

    return text.strip()


def normalize_transmission(raw: str | None) -> str | None:
    """Normalize transmission to 'manual' or 'automatic' only."""
    if raw is None:
        return None

    raw_lower = raw.lower()

    auto_keywords = [
        "automatic", "auto", "dct", "pdk", "dual-clutch", "dual clutch",
        "sequential", "tiptronic", "s-tronic", "smg", "f1", "e-gear",
        "isr", "robotized", "robotised", "cvt", "torque converter",
        "semi-auto", "semi auto", "paddle",
    ]
    manual_keywords = ["manual", "stick", "h-pattern"]

    if re.search(r'\d[- ]speed\s+manual', raw_lower):
        return "manual"
    if re.search(r'\d[- ]speed\s+(?:' + '|'.join(auto_keywords) + ')', raw_lower):
        return "automatic"

    for kw in auto_keywords:
        if kw in raw_lower:
            return "automatic"
    for kw in manual_keywords:
        if kw in raw_lower:
            return "manual"

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
