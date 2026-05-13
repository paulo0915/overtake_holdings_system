"""Parse DuPont GraphQL rows + detail-page description / JSON-LD."""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any

from shared.normalizer import extract_description_fields, normalize_engine_spec, normalize_transmission

from .config import CA_PROVINCES, DETAIL_URL_TEMPLATE, SITE_ORIGIN

logger = logging.getLogger(__name__)

# ``"@context":"https://schema.org","@type":"Car"`` is written into the page as
# an escaped JSON string inside a React Server Components payload. Find the
# opening brace by backing up from the anchor, balance braces to grab the full
# object, then unescape twice (JS string → JSON).
#
# Anchor is the *double-escaped* form because it sits inside ``self.__next_f.push([1,"...\\\"@type\\\":\\\"Car\\\"..."])``
# — the outer push is a JS string with ``\"`` escapes.
_JSONLD_ANCHOR = '\\"@type\\":\\"Car\\"'

# React Server Component pushes; shape is:
#   self.__next_f.push([1,"<body>"])
# where ``<body>`` is a JS-escaped string. The chunk-id / size prefix
# (``33:T52f,``) is sometimes in the same push and sometimes in a previous
# one — we don't need it; we scan every push for prose and pick the best.
_NEXT_F_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])+)"\]\)',
    re.S,
)

# Strip an optional ``<id>:T<hex>,`` prefix so scoring sees just the prose.
_T_CHUNK_PREFIX_RE = re.compile(r"^\d+:T[0-9a-f]+,")


def build_listing_url(
    *, brand_alias: str, model_alias: str, year: int, vin: str, car_id: str,
) -> str:
    return DETAIL_URL_TEMPLATE.format(
        origin=SITE_ORIGIN,
        brand_alias=brand_alias,
        model_alias=model_alias,
        year=year,
        vin=vin,
        car_id=car_id,
    )


def _country_from_state(state: str | None) -> str:
    if state and state.upper() in CA_PROVINCES:
        return "Canada"
    return "USA"


def parse_list_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a single GraphQL ``cars.data[i]`` document into our record shape.

    Returns ``None`` for rows missing an ID / VIN / price — classifieds need at
    least those three to be useful.
    """
    car_id = str(row.get("id") or "").strip()
    vin = (row.get("vin") or "").strip() or None
    price = row.get("price")
    if not car_id or price is None:
        return None

    brand = row.get("brand") or {}
    model = row.get("model") or {}
    brand_name = (brand.get("name") or "").strip() or None
    brand_alias = (brand.get("alias") or "").strip()
    model_name = (model.get("name") or "").strip() or None
    model_alias = (model.get("alias") or "").strip()
    year = row.get("year")

    if not brand_alias or not model_alias or not vin or year is None:
        # Without these we can't build a stable listing URL — drop.
        return None

    title = " ".join(
        str(p) for p in (year, brand_name, model_name) if p
    ).strip()

    loc = row.get("locations") or {}
    state = (loc.get("state") or "").strip() or None
    city = (loc.get("city") or "").strip() or None
    country = _country_from_state(state)

    dealer = row.get("dealer") or {}
    dealer_name = (dealer.get("name") or "").strip() or None

    ext = (row.get("exteriorColor") or {}).get("name")
    intc = (row.get("interiorColor") or {}).get("name")

    mileage = row.get("mileage")
    # DuPont stores mileage as an integer already in miles (US-dominant feed);
    # Canadian rows are already converted by dealers before upload.
    mileage_mi = int(mileage) if isinstance(mileage, (int, float)) and mileage >= 0 else None

    updated_at = row.get("updatedAt")
    created_at = row.get("createdAt")
    listing_url = build_listing_url(
        brand_alias=brand_alias,
        model_alias=model_alias,
        year=int(year),
        vin=vin,
        car_id=car_id,
    )

    main_image = (row.get("mainImageUrl") or "").strip() or None

    return {
        "record_id": f"dpr_{car_id}",
        "car_id": car_id,
        "title": title or None,
        "year": int(year) if year is not None else None,
        "make": brand_name,
        "model": model_name,
        "brand_alias": brand_alias,
        "model_alias": model_alias,
        "vin": vin,
        "mileage_mi": mileage_mi,
        "ext_color": ext,
        "int_color": intc,
        "dealer_name": dealer_name,
        "asking_price": int(price),
        "currency": "USD",
        "country": country,
        "city": city,
        "state": state,
        "image_url": main_image,
        "listing_url": listing_url,
        "created_at_ms": int(created_at) if str(created_at or "").isdigit() else None,
        "updated_at_ms": int(updated_at) if str(updated_at or "").isdigit() else None,
    }


def _unescape_rsc_string(s: str) -> str:
    """React Server Components pushes contain JSON-escaped strings.

    Reverse the common escape sequences so regex can run on readable text.
    """
    return (
        s.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\/", "/")
    )


def extract_jsonld_car(html: str) -> dict[str, Any] | None:
    """Pull the ``@type: Car`` JSON-LD blob — DuPont embeds it as an escaped
    string inside an RSC push, not as a ``<script type="application/ld+json">``
    tag, so standard HTML parsers don't find it.
    """
    if not html:
        return None
    idx = html.find(_JSONLD_ANCHOR)
    if idx < 0:
        return None
    # Walk backwards to the opening brace.
    i = idx
    while i > 0 and html[i] != "{":
        i -= 1
    start = i
    # Forward-scan balancing braces (respect escape + string context).
    depth = 0
    in_string = False
    escape = False
    end = start
    for j in range(start, min(len(html), start + 20000)):
        c = html[j]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    raw = html[start:end]
    try:
        return json.loads(_unescape_rsc_string(raw))
    except json.JSONDecodeError as e:
        logger.debug("JSON-LD parse failed: %s", e)
        return None


def extract_long_description(html: str) -> str | None:
    """Return the longest ``self.__next_f.push([1,"..."])`` body that reads
    like human prose (has spaces, not JSON/JSX boilerplate). Strips a leading
    ``<id>:T<hex>,`` chunk header when present.
    """
    if not html:
        return None
    best: tuple[int, str] | None = None
    for m in _NEXT_F_PUSH_RE.finditer(html):
        body = _unescape_rsc_string(m.group(1))
        # Strip ``<id>:T<hex>,`` (description chunk) or ``<id>:[[..`` (JSX array ref).
        body_no_prefix = _T_CHUNK_PREFIX_RE.sub("", body)
        # Skip React Server Component JSX array payloads that also start with
        # ``<id>:[[...``. Those look like long prose (many spaces) but aren't.
        if re.match(r"^\d+:\[", body) or re.match(r"^\d+:I\[", body):
            continue
        body = body_no_prefix
        if len(body) < 200:
            continue
        stripped = body.lstrip()
        # Skip bare JSON/JSX payloads.
        if stripped.startswith(("{", "[", "I[", "$")):
            continue
        # Reject bodies that are mostly JSX: lots of ``","`` and ``[[``.
        jsx_markers = body.count('","') + body.count("[[") + body.count("null")
        if jsx_markers > 5:
            continue
        # Basic prose sanity: many spaces, at least one real sentence.
        if body.count(" ") < 20:
            continue
        score = len(body)
        lower = body.lower()
        if "price in usd" in lower or "mileage" in lower or "paint" in lower:
            score += 10_000
        if best is None or score > best[0]:
            best = (score, body)
    return best[1].strip() if best else None


# Transmission context — only count the hit when it's adjacent to a gearbox
# keyword. "Manual seats" must NOT match; "manual transmission" / "6-speed
# manual" / "PDK gearbox" must match. Non-capturing outer group lets us return
# the canonical term via the capturing group.
_TRANS_HINT_RE = re.compile(
    r"\b(?:"
    r"(manual|automatic)\s+(?:transmission|gearbox|transaxle)"
    r"|(\d[-\s]?speed)\s+(manual|automatic|dct|pdk|dual[-\s]?clutch)"
    r"|(tiptronic|pdk|dct|dual[-\s]?clutch|cvt|s[-\s]?tronic)\b"
    r")",
    re.I,
)
_ENGINE_HINT_RE = re.compile(
    r"\b(\d(?:\.\d+)?\s*[- ]?(?:liter|litre|l)\s+(?:v\d{1,2}|flat[-\s]?\d|"
    r"straight[-\s]?\d|inline[-\s]?\d|boxer[-\s]?\d|twin[-\s]?turbo|"
    r"turbo(?:charged)?|supercharged))\b",
    re.I,
)


def extract_transmission_from_description(desc: str | None) -> str | None:
    """Best-effort ``Manual`` / ``Automatic`` pull from free-text description.

    Only counts transmission-context mentions (``manual transmission``,
    ``6-speed manual``, ``PDK gearbox``) — avoids false positives from prose
    like ``manual seats`` or ``automatic climate control``.
    """
    if not desc:
        return None
    m = _TRANS_HINT_RE.search(desc)
    if not m:
        return None
    # Pick whichever sub-group actually captured.
    raw = next((g for g in m.groups() if g), None)
    return normalize_transmission(raw)


def extract_engine_from_description(desc: str | None) -> str | None:
    """Match a compact displacement+layout phrase (``6.5L V12``, ``3.9L Twin Turbo``)."""
    if not desc:
        return None
    m = _ENGINE_HINT_RE.search(desc)
    if not m:
        return None
    return normalize_engine_spec(m.group(1))


def parse_detail_html(html: str) -> dict[str, Any]:
    """Return ``{description, ext_color, int_color, transmission, engine_spec}``
    from the detail HTML. Any missing fields come back as ``None``.
    """
    jsonld = extract_jsonld_car(html) or {}
    long_desc = extract_long_description(html)

    # Prefer the long RSC body, fall back to the short JSON-LD auto-blurb.
    description = long_desc or (jsonld.get("description") or None)
    if description:
        description = html_lib.unescape(description).strip() or None

    ext = jsonld.get("color") or None
    intc = jsonld.get("vehicleInteriorColor") or None

    # Use the shared multi-pattern extractor (covers "twin-turbo V12", "6.5-liter
    # naturally aspirated V12", "PDK dual-clutch", etc.) — more patterns than the
    # old local regexes which required displacement immediately before V-config.
    desc_fields = extract_description_fields(description or "") if description else {}
    engine_spec = desc_fields.get("engine")
    transmission = desc_fields.get("transmission")

    # JSON-LD vehicleTransmission is populated by some dealers — use as fallback.
    if not transmission:
        jt = jsonld.get("vehicleTransmission")
        if jt:
            transmission = normalize_transmission(str(jt))

    # JSON-LD vehicleEngine is a schema.org EngineSpecification dict on some listings.
    if not engine_spec:
        je = jsonld.get("vehicleEngine")
        if isinstance(je, dict):
            disp = je.get("engineDisplacement") or {}
            val = disp.get("value") if isinstance(disp, dict) else None
            unit_raw = (disp.get("unitText") or "").strip().lower() if isinstance(disp, dict) else ""
            etype = (je.get("engineType") or "").strip()
            if val:
                try:
                    num = float(str(val).replace(",", ""))
                    if unit_raw in ("cc", "cm3", "cm³"):
                        disp_str = f"{num / 1000:.1f}-Liter"
                    elif unit_raw in ("l", "liter", "liters", "litre", "litres"):
                        disp_str = f"{num:.1f}-Liter"
                    else:
                        disp_str = str(val)
                except (TypeError, ValueError):
                    disp_str = str(val)
                raw = f"{disp_str} {etype}".strip()
                engine_spec = normalize_engine_spec(raw) or raw

    return {
        "description": description,
        "ext_color": ext,
        "int_color": intc,
        "transmission": transmission,
        "engine_spec": engine_spec,
    }
