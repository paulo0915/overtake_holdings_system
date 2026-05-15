"""Algolia hit normalization + Mecum lot ``__NEXT_DATA__`` parsing.

**VIN column:** filled from GraphQL ``post.vinSerial`` when the catalog has one; if that is blank
but ``post.frameNumber`` looks like an identifier, we use it. Many pre-1981 or race cars have no
17-char VIN on the API — then we optionally parse ``listing_description`` (VIN / chassis / serial
labels, or a 17-character VIN token).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from shared.normalizer import normalize_engine_spec, normalize_transmission

from .config import MECUM_SITE_ORIGIN

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _taxonomy_first(tax: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if not isinstance(tax, dict):
        return None
    arr = tax.get(key)
    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
        return arr[0]
    return None


def _auction_display_name(hit: dict[str, Any]) -> str | None:
    a = _taxonomy_first(hit.get("taxonomies") if isinstance(hit.get("taxonomies"), dict) else None, "auction_tax")
    if not a:
        return None
    name = a.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    # "Kissimmee 2019|1546473600|1547337600" -> "Kissimmee 2019"
    return name.split("|", 1)[0].strip()


def _unix_to_date(ts: Any) -> str | None:
    try:
        t = int(ts)
        if t <= 0:
            return None
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _money_int(raw: Any) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not s.lstrip("-").isdigit():
        return None
    return int(s)


def _first_nonempty(*vals: str) -> str | None:
    for v in vals:
        if v:
            return v
    return None


def _year_from_hit(hit: dict[str, Any]) -> int | None:
    ly = _taxonomy_first(hit.get("taxonomies") if isinstance(hit.get("taxonomies"), dict) else None, "lot_year")
    if not ly:
        return None
    n = ly.get("name")
    if isinstance(n, int):
        return n if 1885 <= n <= 2035 else None
    if isinstance(n, str) and n.strip().isdigit():
        y = int(n.strip())
        return y if 1885 <= y <= 2035 else None
    return None


_SECTION_HEADERS = (
    "SPECIFICATIONS",
    "ENGINE",
    "TRANSMISSION",
    "EXTERIOR COLOR",
    "INTERIOR COLOR",
    "MILEAGE",
    "ODOMETER",
    "HIGHLIGHTS",
    "GALLERY",
    "VIDEO",
    "PROVENANCE",
    "OPTIONS",
    "CHASSIS",
)


def _collect_block_html(blocks: Any, depth: int = 0) -> str:
    if not isinstance(blocks, list) or depth > 28:
        return ""
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        attrs = b.get("attributes")
        if isinstance(attrs, dict):
            h = attrs.get("html")
            if isinstance(h, str) and h.strip():
                parts.append(h.strip())
        inner = b.get("innerBlocks")
        if inner:
            inner_h = _collect_block_html(inner, depth + 1)
            if inner_h:
                parts.append(inner_h)
    return "\n".join(parts)


def _sections_from_plain_lines(text: str) -> dict[str, str]:
    """Split flattened lot copy on section headers (works without ``attributes.html``)."""
    if not text or not str(text).strip():
        return {}
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return _lines_into_sections(lines)


def _lines_into_sections(lines: list[str]) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    cur = "_"
    for ln in lines:
        ul = ln.upper().strip()
        hit = None
        for h in sorted(_SECTION_HEADERS, key=len, reverse=True):
            if ul == h:
                hit = h
                break
        if hit:
            cur = hit
            sections.setdefault(cur, [])
            continue
        sections.setdefault(cur, []).append(ln)
    return {k: " ".join(v).strip() for k, v in sections.items() if k != "_" and v}


def _sections_from_lot_block_html(html_blob: str) -> dict[str, str]:
    """Split HTML from Gutenberg ``attributes.html`` into labeled sections."""
    if not html_blob or not str(html_blob).strip():
        return {}
    soup = BeautifulSoup(html_blob, "lxml")
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return _lines_into_sections(lines)


_SECTION_STOP_MARKERS = (
    " HIGHLIGHTS ",
    " THE STORY ",
    " TRUNK ",
    " GALLERY ",
    " VIDEO ",
    " PROVENANCE ",
    " TRANSMISSION ",
    " EXTERIOR COLOR ",
    " INTERIOR COLOR ",
    " MILEAGE ",
    " ODOMETER ",
    " EQUIPMENT ",
    " DOCUMENTATION ",
    " HIGHLIGHT ",
    " HISTORIC ",
)


def _tidy_section_body(raw: str, *, max_len: int = 280) -> str:
    """Strip tags / collapse whitespace; cut at the earliest obvious next-section marker."""
    if not raw or not str(raw).strip():
        return ""
    s = raw.strip()
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "lxml").get_text(" ", strip=True)
    else:
        s = re.sub(r"\s+", " ", s).strip()
    up = f" {s.upper()} "
    cuts: list[int] = []
    for m in _SECTION_STOP_MARKERS:
        ix = up.find(m)
        if ix > 5:
            cuts.append(ix)
    if cuts:
        s = s[: min(cuts)].strip()
    return s[:max_len].strip()


def _mileage_from_section(text: str | None) -> int | None:
    if not text:
        return None
    # Remove digit-grouping commas ("11,000" → "11000") without breaking surrounding text.
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    # Adjective between number and unit first ("11000 original miles", "45000 actual miles")
    # so a later plain "N miles" in narrative text doesn't shadow the specific mileage claim.
    m = re.search(
        r"(?:^|\s)(\d{1,7})\s+(?:original|actual|indicated|documented|verified|showing|approximately|approx\.?)\s+(?:mi\.?|miles?|mile)\b",
        t, re.I,
    )
    if m:
        return int(m.group(1))
    # Direct: "11000 miles" or "11000 mi"
    m = re.search(r"(?:^|\s)(\d{1,7})\s*(?:mi\.?|miles?|mile)\b", t, re.I)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(?:^|\s)(\d{1,7})\s*(?:km|kilometers?|kilometres?)\b", t, re.I)
    if m2:
        return round(int(m2.group(1)) * 0.621371)
    if re.search(r"\b(?:actual|mileage|odometer)\b", t, re.I):
        m3 = re.search(r"\b(\d{1,7})\b", t)
        if m3:
            return int(m3.group(1))
    return None


def _make_model_from_hit(hit: dict[str, Any]) -> tuple[str | None, str | None]:
    tax = hit.get("taxonomies") if isinstance(hit.get("taxonomies"), dict) else {}
    mk = _taxonomy_first(tax, "make")
    md = _taxonomy_first(tax, "model")
    make = (mk.get("name") or "").strip() if mk else None
    model = (md.get("name") or "").strip() if md else None
    return (make or None, model or None)


def parse_algolia_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one Algolia lot document from search ``hits``."""
    if not isinstance(hit, dict):
        return None
    if (hit.get("post_type") or "").lower() != "lot":
        return None
    web_id = hit.get("web_id_meta")
    if web_id is None or str(web_id).strip() == "":
        return None
    web_id_s = str(web_id).strip()
    perm = (hit.get("permalink") or "").strip()
    if not perm:
        return None

    hammer = _money_int(hit.get("hammer_price_meta"))
    if hammer is None:
        hammer = _money_int(hit.get("highest_bid_or_price"))

    imgs = hit.get("images_meta")
    image_url = None
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, dict) and first.get("url"):
            image_url = str(first["url"]).strip()

    year = _year_from_hit(hit)
    make, model = _make_model_from_hit(hit)
    end_ts = hit.get("auction_end_date_meta")
    rd = _taxonomy_first(hit.get("taxonomies") if isinstance(hit.get("taxonomies"), dict) else None, "run_date")
    run_ts = rd.get("timestamp") if rd else None
    selling_date = _unix_to_date(end_ts) or _unix_to_date(run_ts)

    return {
        "web_id": web_id_s,
        "post_id": hit.get("post_id"),
        "title": (hit.get("post_title") or "").strip() or None,
        "year": year,
        "make": make,
        "model": model,
        "permalink": perm,
        "listing_url": f"{MECUM_SITE_ORIGIN}{perm}" if perm.startswith("/") else f"{MECUM_SITE_ORIGIN}/{perm}",
        "hammer_usd": hammer,
        "lot_number": (hit.get("lot_number_meta") or "").strip() or None,
        "ext_color": _first_nonempty(
            (hit.get("color_meta") or "").strip(),
            (hit.get("generic_exterior_color_meta") or "").strip(),
        ),
        "int_color": _first_nonempty(
            (hit.get("interior_meta") or "").strip(),
            (hit.get("generic_interior_color_meta") or "").strip(),
        ),
        "engine_spec": normalize_engine_spec((hit.get("engine_configuration_meta") or "").strip())
        or ((hit.get("engine_configuration_meta") or "").strip() or None),
        "transmission": normalize_transmission(hit.get("transmission_type_meta")),
        "image_url": image_url,
        "selling_date": selling_date,
        "auction_event": _auction_display_name(hit),
    }


def _connection_node_names(conn: Any) -> list[str]:
    if not isinstance(conn, dict):
        return []
    out: list[str] = []
    for e in conn.get("edges") or []:
        if isinstance(e, dict):
            node = e.get("node")
            if isinstance(node, dict) and node.get("name") is not None:
                out.append(str(node["name"]).strip())
    return [x for x in out if x]


def _sale_slug(post: dict[str, Any]) -> str | None:
    sr = post.get("saleResults")
    if not isinstance(sr, dict):
        return None
    names = _connection_node_names(sr)
    if not names:
        return None
    return names[0].lower().replace(" ", "-")


def _vin_token_plausible(s: str) -> bool:
    t = re.sub(r"[\s\-]", "", (s or "").strip())
    if len(t) < 4 or len(t) > 25:
        return False
    if t.isdigit() or not any(c.isalpha() for c in t):
        return False
    if re.fullmatch(r"19\d{2}|20\d{2}", t):
        return False
    return True


_VIN_17_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b", re.I)


def _blocks_to_text(blocks: Any, depth: int = 0) -> str:
    if not isinstance(blocks, list) or depth > 30:
        return ""
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        attrs = b.get("attributes")
        if isinstance(attrs, dict):
            for key in ("text", "content", "html"):
                v = attrs.get(key)
                if isinstance(v, str) and v.strip() and v.strip() not in ("Lot header",):
                    parts.append(v.strip())
        inner = b.get("innerBlocks")
        if inner:
            inner_txt = _blocks_to_text(inner, depth + 1)
            if inner_txt:
                parts.append(inner_txt)
    return "\n\n".join(parts)


def parse_lot_post(post: dict[str, Any]) -> dict[str, Any]:
    """Extract fields from GraphQL ``Lot`` shape under ``pageProps.post``."""
    title = (post.get("title") or "").strip() or None
    lot_number = (post.get("lotNumber") or "").strip() or None
    transmission = normalize_transmission(post.get("transmission"))

    makes = _connection_node_names(post.get("makes"))
    models = _connection_node_names(post.get("models"))
    years = _connection_node_names(post.get("lotYears"))
    make = makes[0] if makes else None
    model = models[0] if models else None
    year: int | None = None
    if years and years[0].isdigit():
        y = int(years[0])
        year = y if 1885 <= y <= 2035 else None

    odo_raw = (post.get("odometer") or "").strip()
    mileage_mi: int | None = None
    if odo_raw.isdigit():
        mileage_mi = int(odo_raw)
    elif odo_raw.replace(",", "").isdigit():
        mileage_mi = int(odo_raw.replace(",", ""))

    units = (post.get("odometerUnits") or "").strip().lower()
    if mileage_mi is not None:
        if units in ("km", "kilometers", "kilometre", "kilometres", "k"):
            mileage_mi = round(mileage_mi * 0.621371)
        # "m" alone = miles on Mecum; do not treat as meters.

    # Primary identifier from Mecum GraphQL ``Lot`` (public catalog field).
    vin = (post.get("vinSerial") or "").strip() or None
    if not vin:
        fn = (post.get("frameNumber") or "").strip()
        if fn and _vin_token_plausible(fn):
            vin = re.sub(r"\s+", "", fn.upper())
    ext_color = (post.get("color") or "").strip() or None
    int_color = (post.get("interior") or "").strip() or None
    eng = (post.get("engineNumber") or "").strip() or None
    engine_spec = normalize_engine_spec(eng) or eng

    hammer = _money_int(post.get("hammerPrice"))

    excerpt = (post.get("excerpt") or "").strip()
    blocks_txt = _blocks_to_text(post.get("blocks"))
    desc_parts = [p for p in (excerpt, blocks_txt) if p]
    listing_description = "\n\n".join(desc_parts) if desc_parts else None

    imgs = post.get("images")
    image_url = None
    if isinstance(imgs, list) and imgs:
        first_img = imgs[0]
        if isinstance(first_img, dict) and first_img.get("url"):
            image_url = str(first_img["url"]).strip()
    elif isinstance(imgs, dict):
        edges = imgs.get("edges") or []
        if isinstance(edges, list) and edges:
            n0 = (edges[0] or {}).get("node") if isinstance(edges[0], dict) else None
            if isinstance(n0, dict):
                for k in ("sourceUrl", "mediaItemUrl", "url"):
                    if n0.get(k):
                        image_url = str(n0[k]).strip()
                        break

    run_dates = post.get("runDates")
    selling_date: str | None = None
    if isinstance(run_dates, dict):
        rnames = _connection_node_names(run_dates)
        if rnames:
            raw = rnames[0][:10]
            if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                selling_date = raw

    sold_slug = _sale_slug(post)
    sold_status = "Sold" if sold_slug == "sold" else ("Unsold" if sold_slug else "Sold")

    hide_hammer = bool(post.get("hideHammerPrice"))
    if hide_hammer:
        hammer = None

    html_blob = _collect_block_html(post.get("blocks"))
    sec: dict[str, str] = {}
    if blocks_txt:
        sec.update(_sections_from_plain_lines(blocks_txt))
    if html_blob:
        for k, v in _sections_from_lot_block_html(html_blob).items():
            if v:
                sec[k] = v

    eng_sec = _tidy_section_body(sec.get("ENGINE") or "", max_len=320)
    if not (engine_spec or "").strip() and eng_sec:
        engine_spec = normalize_engine_spec(eng_sec) or eng_sec

    trans_sec = _tidy_section_body(sec.get("TRANSMISSION") or "", max_len=160)
    if not transmission and trans_sec:
        # Enum-only: leave NULL if the normalizer can't pick a canonical bucket.
        transmission = normalize_transmission(trans_sec)

    mile_sec = sec.get("MILEAGE") or sec.get("ODOMETER")
    if mileage_mi is None:
        m_from_blk = _mileage_from_section(_tidy_section_body(mile_sec or "", max_len=400))
        if m_from_blk is not None:
            mileage_mi = m_from_blk
    # Fallback: scan HIGHLIGHTS bullets for a mileage figure (e.g. "11,000 original miles").
    # _tidy_section_body cuts at "THE STORY" so narrative text doesn't pollute the search.
    if mileage_mi is None:
        highlights_body = _tidy_section_body(sec.get("HIGHLIGHTS") or "", max_len=800)
        if highlights_body:
            m_from_hl = _mileage_from_section(highlights_body)
            if m_from_hl is not None:
                mileage_mi = m_from_hl

    col_sec = _tidy_section_body(sec.get("EXTERIOR COLOR") or "", max_len=200)
    if not (ext_color or "").strip() and col_sec:
        ext_color = col_sec

    int_sec = _tidy_section_body(sec.get("INTERIOR COLOR") or "", max_len=200)
    if not (int_color or "").strip() and int_sec:
        int_color = int_sec

    return {
        "title": title,
        "year": year,
        "make": make,
        "model": model,
        "lot_number": lot_number,
        "transmission": transmission,
        "mileage_mi": mileage_mi,
        "vin": vin,
        "ext_color": ext_color,
        "int_color": int_color,
        "engine_spec": engine_spec,
        "hammer_usd": hammer,
        "image_url": image_url,
        "listing_description": listing_description,
        "selling_date": selling_date,
        "sold_status": sold_status,
    }


def _listing_text_plain(desc: str | None) -> str:
    if not desc or not str(desc).strip():
        return ""
    s = str(desc).strip()
    if "<" in s and ">" in s:
        return BeautifulSoup(s, "lxml").get_text("\n")
    return s


def extract_transmission_from_description(desc: str | None) -> str | None:
    """Parse transmission from lot copy.

    Tries a dedicated ``TRANSMISSION`` section first, then falls back to scanning
    the full plain text for recognizable patterns (e.g. bullets like
    "5-speed manual transmission" that live under ``HIGHLIGHTS``).
    """
    plain = _listing_text_plain(desc)
    if not plain.strip():
        return None
    sec = _sections_from_plain_lines(plain)
    raw = _tidy_section_body(sec.get("TRANSMISSION") or "", max_len=220)
    if raw:
        nt = normalize_transmission(raw)
        if nt:
            return nt
        rl = raw.lower()
        if any(k in rl for k in ("gearbox", "transaxle", "speed", "cvt", "dsg", "pdk")):
            return raw[:120]
    # Fallback: scan entire plain text for clear transmission signals.
    # Catches bullets like "5-speed manual transmission" buried under HIGHLIGHTS.
    return normalize_transmission(plain)


def extract_vin_from_description(desc: str | None) -> str | None:
    """Find a VIN / serial / chassis token in narrative when structured fields are blank."""
    plain = _listing_text_plain(desc)
    if len(plain) < 14:
        return None
    m = _VIN_17_RE.search(plain)
    if m:
        return m.group(1).upper()
    patterns = (
        r"(?is)\bvin\s*[#:.]?\s*([A-HJ-NPR-Z0-9](?:[A-HJ-NPR-Z0-9\s\-]{3,23})[A-HJ-NPR-Z0-9])\b",
        r"(?is)\bvehicle\s+identification\s*(?:number|no\.?)\s*[#:.]?\s*([A-HJ-NPR-Z0-9](?:[\w\-]{3,23})\w)\b",
        r"(?is)\bchassis\s*(?:number|no\.?)?\s*[#:.]?\s*([A-Z0-9](?:[A-Z0-9\-]{2,23})[A-Z0-9])\b",
        r"(?is)\bserial\s*(?:number|no\.?)?\s*[#:.]?\s*([A-Z0-9](?:[A-Z0-9\-]{3,23})[A-Z0-9])\b",
    )
    for pat in patterns:
        mm = re.search(pat, plain)
        if not mm:
            continue
        cand = re.sub(r"\s+", "", mm.group(1).strip().upper())
        if _vin_token_plausible(cand):
            return cand[:32]
    return None


def enrich_vin_and_transmission_from_description(
    merged: dict[str, Any],
    listing_text: str | None,
) -> None:
    """Fill missing ``vin`` / ``transmission`` from lot narrative (after API + section parse)."""
    if not listing_text or not str(listing_text).strip():
        return
    if not merged.get("transmission"):
        t = extract_transmission_from_description(listing_text)
        if t:
            merged["transmission"] = t
    if not merged.get("vin"):
        v = extract_vin_from_description(listing_text)
        if v:
            merged["vin"] = v


_HTML_MILEAGE_RE = re.compile(
    r"(?:^|[\s(])(\d[\d,]{0,6})\s*(?:original|actual|indicated|documented|showing|approximately|approx\.?)?\s*(?:miles?|mi\.?)\b",
    re.I,
)
_HTML_KM_RE = re.compile(
    r"(?:^|[\s(])(\d[\d,]{0,6})\s*(?:km|kilometers?|kilometres?)\b",
    re.I,
)


_HTML_VIN_PLACEHOLDER = frozenset({"N/A", "TBD", "TBC", "NONE", "-", "NA", "UNKNOWN", ""})


def _html_vin_fallback(soup: BeautifulSoup) -> str | None:
    """Scan rendered HTML for the VIN/Serial block Mecum renders in the lot header."""
    # <div class="LotHeader-module__...__odometerSerial">
    #   <div><p>VIN / Serial</p><p>10913</p></div>
    # </div>
    for el in soup.find_all(class_=lambda c: c and "odometerSerial" in " ".join(c) if isinstance(c, list) else "odometerSerial" in (c or "")):
        ps = el.find_all("p")
        for i, p in enumerate(ps):
            label = p.get_text(strip=True).lower()
            if "vin" in label or "serial" in label or "chassis" in label or "frame" in label:
                if i + 1 < len(ps):
                    val = ps[i + 1].get_text(strip=True)
                    if not val or val.upper() in _HTML_VIN_PLACEHOLDER:
                        continue
                    cand = re.sub(r"[\s\-]+", "", val).upper()
                    # Accept standard alphanumeric chassis OR purely-numeric serials (e.g. "10913").
                    # _vin_token_plausible rejects all-digit strings, so check separately.
                    if _vin_token_plausible(cand):
                        return cand
                    if cand.isdigit() and 4 <= len(cand) <= 25:
                        return cand
    return None


def _html_mileage_fallback(soup: BeautifulSoup) -> int | None:
    """Extract mileage from rendered <li> or <p> elements (e.g. '194 miles', '32,000 km')."""
    for tag in soup.find_all(["li", "p", "span", "td"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 120:
            continue
        cleaned = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
        m = _HTML_MILEAGE_RE.search(cleaned)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m = _HTML_KM_RE.search(cleaned)
        if m:
            try:
                return round(int(m.group(1).replace(",", "")) * 0.621371)
            except ValueError:
                pass
    return None


_DESC_SKIP_HEADINGS = {"SPECIFICATIONS", "PHOTOS", "VIDEOS", "DOCUMENTATION"}


def _specs_from_article(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract structured spec values from the SPECIFICATIONS section.

    The new Mecum DOM renders specs as ``wp-block-column`` divs whose
    direct text is ``"<LABEL> <VALUE>"`` and whose ``<strong>`` child
    holds just ``<VALUE>``. We get LABEL by stripping the strong text
    from the full text.

    Mecum's GraphQL ``post`` no longer reliably populates engine_spec
    (the ``engineNumber`` field stores serials, not displacement strings),
    and the description-scan fallback only catches ~8% of lots. The
    SPECIFICATIONS panel has the canonical values for ~all sold lots.
    """
    out: dict[str, Any] = {}
    article = soup.find("article")
    if not article:
        return out
    spec_section = None
    for sec in article.select("[class*=lotSection]"):
        head = sec.get_text(" ", strip=True).split(" ", 1)[0].upper()
        if head == "SPECIFICATIONS":
            spec_section = sec
            break
    if spec_section is None:
        return out

    # ``LABEL_TO_FIELD`` maps the human-visible heading to our normalized
    # internal key. Add new entries here if Mecum surfaces more spec rows.
    LABEL_TO_FIELD = {
        "ENGINE": "engine_spec",
        "TRANSMISSION": "transmission",
        "EXTERIOR COLOR": "ext_color",
        "INTERIOR COLOR": "int_color",
    }
    for col in spec_section.select(".wp-block-column, [class*=column]"):
        strong = col.find("strong")
        if not strong:
            continue
        value = strong.get_text(" ", strip=True)
        if not value:
            continue
        full = col.get_text(" ", strip=True)
        # Label = full text minus the strong-value at the end.
        label = full[: -len(value)].strip().upper().rstrip(":")
        field = LABEL_TO_FIELD.get(label)
        if field and field not in out:
            out[field] = value
    return out


def _description_from_article(soup: BeautifulSoup) -> str | None:
    """Mecum's App Router rebuild dropped __NEXT_DATA__. The editorial body
    is now rendered as discrete sections inside ``<article>``: HIGHLIGHTS,
    THE STORY, EQUIPMENT, and lot-specific history blocks. Concatenate them.
    """
    article = soup.find("article")
    if not article:
        return None
    chunks: list[str] = []
    for el in article.select("[class*=lotSection], [class*=Group-module]"):
        text = el.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue
        first_word = text.split(" ", 1)[0].upper()
        if first_word in _DESC_SKIP_HEADINGS:
            continue
        chunks.append(text)
    if not chunks:
        return None
    seen: set[str] = set()
    deduped: list[str] = []
    for c in chunks:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
    return "\n\n".join(deduped)[:50_000]


def parse_lot_html(html: str | None) -> dict[str, Any] | None:
    """Parse lot detail page HTML into a dict overlay (or ``None``)."""
    if not html or not str(html).strip():
        return None

    result: dict[str, Any] = {}
    m = _NEXT_DATA_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            data = {}
        post = (data.get("props") or {}).get("pageProps", {}).get("post")
        if isinstance(post, dict):
            parsed = parse_lot_post(post)
            if parsed:
                result = parsed

    # Always try the rendered HTML for VIN/mileage — catches lots whose
    # __NEXT_DATA__ is absent (server-side render variants) or whose
    # GraphQL fields are blank while the visible page still shows the data.
    soup = BeautifulSoup(html, "lxml")
    if not result.get("vin"):
        v = _html_vin_fallback(soup)
        if v:
            result["vin"] = v
    if result.get("mileage_mi") is None:
        mi = _html_mileage_fallback(soup)
        if mi is not None:
            result["mileage_mi"] = mi
    if not result.get("listing_description"):
        desc = _description_from_article(soup)
        if desc:
            result["listing_description"] = desc

    # SPECIFICATIONS section gives us engine_spec / transmission / colors
    # the GraphQL ``post`` doesn't reliably carry. Only fill blanks — never
    # overwrite a value the API already provided.
    specs = _specs_from_article(soup)
    if specs:
        for k, v in specs.items():
            if v and not result.get(k):
                if k == "engine_spec":
                    result[k] = normalize_engine_spec(v) or v
                elif k == "transmission":
                    result[k] = normalize_transmission(v) or v
                else:
                    result[k] = v

    return result if result else None


def merge_hit_and_detail(hit_summary: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """Prefer detail page fields when present; keep Algolia-only fields from ``hit_summary``."""
    out = dict(hit_summary)
    if not detail:
        return out
    for key in (
        "title",
        "year",
        "make",
        "model",
        "lot_number",
        "transmission",
        "mileage_mi",
        "vin",
        "ext_color",
        "int_color",
        "engine_spec",
        "hammer_usd",
        "image_url",
        "listing_description",
        "selling_date",
        "sold_status",
    ):
        v = detail.get(key)
        if key == "mileage_mi":
            if isinstance(v, int):
                out[key] = v
            elif v is not None and v != "":
                out[key] = v
        elif v is not None and v != "":
            out[key] = v
    return out
