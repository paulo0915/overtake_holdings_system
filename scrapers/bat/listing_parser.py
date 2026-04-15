"""Parse individual BaT listing pages for detailed fields."""

import html as html_lib
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Patterns for parts/memorabilia detection
_PARTS_KEYWORDS = re.compile(
    r'\b(?:parts\s+car|memorabilia|scale\s+model|artwork|poster|book\s+set|'
    r'tool\s+kit|luggage\s+set|spare\s+parts|accessories\s+lot|watch|'
    r'helmet|racing\s+suit|steering\s+wheel\s+only)\b',
    re.IGNORECASE,
)

# Authenticity detection
_RECREATION_KEYWORDS = re.compile(
    r'\b(?:re-?creation|replica|evocation)\b',
    re.IGNORECASE,
)

# Mileage in essentials bullets
# Matches: "161 Miles", "22,000 Miles Shown", "48k Kilometers Shown", "3,262 Miles TMU"
# Does NOT match: "Mileage Unknown" or "Total Mileage Unknown" (no number)
_MILEAGE_BULLET_RE = re.compile(
    r'^([\d,]+\.?\d*)\s*(k)?\s*-?\s*(miles?|mi|km|kilometers?|kilometres?)',
    re.IGNORECASE,
)

# Mileage in title: "161-Mile", "2k-Mile", "1,200-Kilometer"
_MILEAGE_TITLE_RE = re.compile(
    r'\b([\d,]+\.?\d*)\s*(k)?\s*-\s*(mile|km|kilometer|kilometre)',
    re.IGNORECASE,
)

# Mileage in description text
_MILEAGE_DESC_PATTERNS = [
    re.compile(r'(?:has|shows?|showing|indicating|with|covered)\s+([\d,]+)\s*(miles?|mi|km|kilometers?|kilometres?)', re.IGNORECASE),
    re.compile(r'([\d,]+)\s*(miles?|mi)\s+(?:from\s+new|since\s+new|shown|indicated)', re.IGNORECASE),
    re.compile(r'([\d,]+)\s*-\s*mile\s+(?:example|car|vehicle)', re.IGNORECASE),
    re.compile(r'(?:odometer|mileage)\s*(?:reads?|shows?|indicates?)?\s*:?\s*([\d,]+)\s*(miles?|mi|km|kilometers?)', re.IGNORECASE),
    re.compile(r'now\s+(?:has|shows?)\s+(?:just\s+)?([\d,]+)\s*(miles?|mi|km|kilometers?)', re.IGNORECASE),
]

# Engine spec pattern
_ENGINE_RE = re.compile(
    r'[\d.]+[- ](?:liter|litre)\b',
    re.IGNORECASE,
)

# Transmission keywords (no bare "speed")
_TRANSMISSION_KEYWORDS_RE = re.compile(
    r'\b(?:transmission|transaxle|gearbox|manual\s+transmission|'
    r'automatic|PDK|DCT|dual[- ]clutch|sequential|tiptronic|'
    r'S-tronic|SMG|e-gear|ISR)\b',
    re.IGNORECASE,
)
_SPEED_TRANS_RE = re.compile(
    r'\b\w+[- ]speed\s+(?:manual|automatic|PDK|DCT|dual[- ]clutch|'
    r'sequential|tiptronic|S-tronic|SMG|F1|e-gear|ISR|robotized)\b',
    re.IGNORECASE,
)


def _unescape(text: str | None) -> str | None:
    """Decode HTML entities like &#038; -> &, &#8217; -> '."""
    if text is None:
        return None
    return html_lib.unescape(text)


class ListingParser:
    """Parse a BaT listing HTML page to extract detailed fields."""

    def parse(self, html: str, url: str) -> dict:
        """Parse listing HTML and return extracted fields."""
        soup = BeautifulSoup(html, "lxml")
        result = {
            "is_valid_listing": True,
            "is_parts": False,
            "authenticity": "Original",
            "make": None,
            "model": None,
            "vin": None,
            "mileage_raw": None,
            "mileage_unit": None,
            "ext_color": None,
            "int_color": None,
            "engine_spec": None,
            "transmission_raw": None,
            "lot_number": None,
            "bid_count": None,
            "year": None,
            "listing_description": None,
        }

        # Validate this is a real BaT listing page (not Cloudflare challenge)
        essentials_div = soup.find("div", class_="essentials")
        if not essentials_div:
            result["is_valid_listing"] = False
            return result

        essentials_items = self._get_essentials_items(essentials_div)
        essentials_kv = self._get_essentials_kv(essentials_div)

        # Listing description (full body text)
        description = self._get_listing_description(soup)
        result["listing_description"] = _unescape(description)

        # Title text for various extractions
        title_text = ""
        h1 = soup.find("h1")
        if h1:
            title_text = h1.get_text(strip=True)

        # Make from structured HTML (group-title-label) — extract early for parts check
        result["make"] = self._extract_group_field(soup, "make")

        # Parts/memorabilia detection — check title, description, AND Make tag
        make_tag = result["make"] or ""
        if (_PARTS_KEYWORDS.search(title_text)
                or (description and _PARTS_KEYWORDS.search(description[:500]))
                or "parts" in make_tag.lower()
                or "automobilia" in make_tag.lower()):
            result["is_parts"] = True

        # Authenticity — only check title (description has too many false positives
        # like "replica livery", "replica tool roll" that aren't replica cars)
        if _RECREATION_KEYWORDS.search(title_text):
            result["authenticity"] = "Re-Creation"

        # Model: everything after Make in the title
        if result["make"] and title_text:
            model = self._extract_model_from_title(title_text, result["make"])
            if not model:
                # Make not in title (e.g. Make=Ford but title says "Saleen S7")
                # Fallback: use BaT's model tag with make prefix stripped
                bat_model = self._extract_group_field(soup, "model")
                if bat_model:
                    # Strip make prefix if present (e.g. "Porsche 911" -> "911")
                    if bat_model.lower().startswith(result["make"].lower()):
                        model = bat_model[len(result["make"]):].strip()
                    else:
                        model = bat_model
                if not model:
                    # Last fallback: everything after year in title
                    model = self._extract_model_after_year(title_text)
            result["model"] = model

        # Year
        result["year"] = self._extract_year(title_text, soup)

        # VIN / Chassis
        result["vin"] = self._extract_vin(essentials_kv, essentials_items, soup)

        # Lot number
        result["lot_number"] = self._extract_lot_number(essentials_kv, essentials_div)

        # Bid count from stats section
        result["bid_count"] = self._extract_bid_count(soup)

        # Mileage priority: title (BaT-verified) -> sidebar -> description
        self._extract_mileage_from_title(title_text, result)

        # Process essentials bullets for car details (mileage only fills if still None)
        self._parse_essentials_bullets(essentials_items, result)

        # Mileage fallback from description
        if result["mileage_raw"] is None and description:
            self._extract_mileage_from_description(description, result)

        # Fallback: colors, engine, transmission from description
        if description:
            self._fallback_from_description(description, result)

        # HTML entity decode all text fields
        for key in ("make", "model", "ext_color", "int_color", "engine_spec",
                     "transmission_raw", "vin"):
            result[key] = _unescape(result[key])

        return result

    def _extract_group_field(self, soup: BeautifulSoup, field: str) -> str | None:
        """
        Extract a value from BaT's group-title-label buttons.

        Uses strong.next_sibling to get the text node directly after <strong>,
        avoiding the fragile string-replace approach.
        """
        for strong in soup.find_all("strong", class_="group-title-label"):
            label = strong.get_text(strip=True).lower()
            if label != field:
                continue
            # The value is the text node immediately after the <strong>
            sibling = strong.next_sibling
            if sibling and isinstance(sibling, str):
                value = sibling.strip()
                if value:
                    return _unescape(value)
            # Fallback: get parent button text minus label
            button = strong.parent
            if button:
                full = button.get_text(strip=True)
                value = full.replace(strong.get_text(strip=True), "", 1).strip()
                if value:
                    return _unescape(value)
        return None

    @staticmethod
    def _extract_model_from_title(title: str, make: str) -> str:
        """
        Extract full model with variant from title after the Make name.

        Title:  "2,600-Mile 2004 Ferrari 360 Challenge Stradale"
        Make:   "Ferrari"
        Result: "360 Challenge Stradale"
        """
        idx = title.lower().find(make.lower())
        if idx == -1:
            return ""
        return title[idx + len(make):].strip()

    @staticmethod
    def _extract_model_after_year(title: str) -> str:
        """
        Last-resort model extraction: everything after the year in title.

        Title:  "158-Mile 2007 Saleen S7 LM"
        Result: "Saleen S7 LM"
        """
        m = re.search(r'\b(?:1[89]\d{2}|20\d{2})\b\s*', title)
        if m:
            return title[m.end():].strip()
        return title.strip()

    def _get_essentials_items(self, essentials_div) -> list[str]:
        """Get all <li> text items from div.essentials."""
        items = []
        for li in essentials_div.find_all("li"):
            text = li.get_text(strip=True)
            if text:
                items.append(text)
        return items

    def _get_essentials_kv(self, essentials_div) -> dict[str, str]:
        """Get key-value pairs from essentials <strong> labels."""
        kv = {}
        for strong in essentials_div.find_all("strong"):
            key = strong.get_text(strip=True).lower().rstrip(":")
            parent = strong.parent
            if not parent:
                continue
            # Use next_sibling for cleaner extraction
            sibling = strong.next_sibling
            if sibling and isinstance(sibling, str):
                value = sibling.strip().lstrip(":").strip()
                if value:
                    kv[key] = value
                    continue
            # Fallback
            parent_text = parent.get_text(strip=True)
            value = parent_text.replace(strong.get_text(strip=True), "", 1).strip().lstrip(":").strip()
            if value:
                kv[key] = value

        # Also check <li> items with "Key: Value" format
        for li in essentials_div.find_all("li"):
            text = li.get_text(strip=True)
            if ":" in text:
                parts = text.split(":", 1)
                key = parts[0].strip().lower()
                value = parts[1].strip()
                if key and value:
                    kv[key] = value
        return kv

    def _get_listing_description(self, soup: BeautifulSoup) -> str | None:
        """Extract the full listing description body text."""
        content = soup.select_one("div.post-excerpt")
        if not content:
            content = soup.select_one("div.listing-post-content")
        if not content:
            content = soup.select_one("div.post-content")
        if content:
            return content.get_text(" ", strip=True)
        return None

    _YEAR_RE = re.compile(
        r'\b(1[89]\d{2}|20\d{2})(?![\d,]*\s*k?\s*-\s*(?:mile|km|kilometer))\b',
        re.IGNORECASE,
    )

    def _extract_year(self, title_text: str, soup: BeautifulSoup) -> int | None:
        """Extract model year, ignoring mileage patterns like '1800-Mile'."""
        if title_text:
            match = self._YEAR_RE.search(title_text)
            if match:
                return int(match.group(1))
        title_tag = soup.find("title")
        if title_tag:
            match = self._YEAR_RE.search(title_tag.get_text())
            if match:
                return int(match.group(1))
        return None

    def _extract_lot_number(self, kv: dict, essentials_div) -> str | None:
        """Extract lot number."""
        if "lot" in kv:
            lot = kv["lot"].strip().lstrip("#")
            if lot:
                return lot
        lot_strong = essentials_div.find("strong", string=re.compile(r"^Lot$", re.IGNORECASE))
        if lot_strong and lot_strong.parent:
            text = lot_strong.parent.get_text(strip=True)
            m = re.search(r'#(\d+)', text)
            if m:
                return m.group(1)
        return None

    def _extract_bid_count(self, soup: BeautifulSoup) -> int | None:
        """Extract bid count from listing stats table."""
        # BaT uses <tr class="listing-stats-stat"> with <td>Bids</td><td>33</td>
        # Must match exact "Bids" label, not "Winning Bid" or "High Bid"
        for tr in soup.find_all("tr", class_="listing-stats-stat"):
            label_td = tr.find("td", class_="listing-stats-label")
            if not label_td:
                continue
            label = label_td.get_text(strip=True)
            if label in ("Bids", "Bid"):
                value_td = label_td.find_next_sibling("td")
                if value_td:
                    m = re.search(r'(\d+)', value_td.get_text(strip=True))
                    if m:
                        return int(m.group(1))
        return None

    def _extract_vin(self, kv: dict, items: list[str], soup: BeautifulSoup) -> str | None:
        """Extract VIN or chassis number."""
        for key in ["chassis", "vin", "chassis number"]:
            if key in kv:
                vin = kv[key].strip()
                if vin and vin.lower() not in ("n/a", "na", "none", "not available"):
                    return vin
        for item in items:
            if item.lower().startswith("chassis"):
                match = re.search(r'chassis\s*:?\s*(.+)', item, re.IGNORECASE)
                if match:
                    vin = match.group(1).strip()
                    if vin and vin.lower() not in ("n/a", "na", "none"):
                        return vin
        # Fallback: search listing content area only (not full page)
        content = (
            soup.select_one("div.post-excerpt")
            or soup.select_one("div.listing-post-content")
            or soup.select_one("div.post-content")
        )
        if content:
            vin_match = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', content.get_text())
            if vin_match:
                return vin_match.group(1)
        return None

    def _parse_essentials_bullets(self, items: list[str], result: dict) -> None:
        """Parse essentials <li> bullets for mileage, colors, engine, transmission."""
        for text in items:
            text = text.strip()
            if not text:
                continue

            # Skip chassis/VIN bullets — Bug #2 fix
            if text.lower().startswith("chassis"):
                continue

            # Mileage: "161 Miles", "48,000 Kilometers"
            if result["mileage_raw"] is None:
                m = _MILEAGE_BULLET_RE.match(text)
                if m:
                    raw_num = m.group(1)
                    if m.group(2):  # 'k' suffix
                        raw_num += "k"
                    result["mileage_raw"] = raw_num
                    result["mileage_unit"] = "km" if m.group(3).lower().startswith("k") else "miles"
                    continue

            # Exterior color: "Rosso Corsa Paint", "Grigio Silverstone Metallic Paint"
            if result["ext_color"] is None:
                if "paint" in text.lower() or "exterior" in text.lower():
                    color = re.sub(r'\s*(?:metallic\s+)?paint\s*$', '', text, flags=re.IGNORECASE)
                    color = re.sub(r'\s*exterior\s*$', '', color, flags=re.IGNORECASE).strip()
                    if color and not _ENGINE_RE.search(color):
                        result["ext_color"] = color
                        continue

            # Interior color: "Black Leather and Alcantara Upholstery" — Bug #3 fix
            if result["int_color"] is None:
                text_lower = text.lower()
                interior_keywords = ["leather", "alcantara", "suede", "upholstery", "nappa", "cloth", "fabric"]
                if any(kw in text_lower for kw in interior_keywords):
                    if not _ENGINE_RE.search(text) and not _SPEED_TRANS_RE.search(text):
                        result["int_color"] = text
                        continue

            # Engine: "6.3-Liter V12 w/Electric Assistance" — Bug #2 fix
            if result["engine_spec"] is None:
                if _ENGINE_RE.search(text):
                    result["engine_spec"] = text
                    continue

            # Transmission: "Seven-Speed Dual-Clutch Transmission"
            if result["transmission_raw"] is None:
                if _SPEED_TRANS_RE.search(text) or _TRANSMISSION_KEYWORDS_RE.search(text):
                    if not _ENGINE_RE.search(text):
                        result["transmission_raw"] = text
                        continue

    def _extract_mileage_from_title(self, title: str, result: dict) -> None:
        """Extract mileage from BaT title format: '161-Mile', '2k-Mile', '1,200-Kilometer'."""
        m = _MILEAGE_TITLE_RE.search(title)
        if m:
            raw_num = m.group(1)
            if m.group(2):  # 'k' suffix
                raw_num += "k"
            result["mileage_raw"] = raw_num
            result["mileage_unit"] = "km" if m.group(3).lower().startswith("k") else "miles"

    def _extract_mileage_from_description(self, description: str, result: dict) -> None:
        """Extract mileage from listing description body text."""
        for pattern in _MILEAGE_DESC_PATTERNS:
            m = pattern.search(description)
            if m:
                result["mileage_raw"] = m.group(1)
                unit_str = m.group(2).lower() if m.lastindex and m.lastindex >= 2 and m.group(2) else "miles"
                result["mileage_unit"] = "km" if unit_str.startswith("k") else "miles"
                return

    def _fallback_from_description(self, description: str, result: dict) -> None:
        """Extract missing fields from listing description text."""
        # Exterior color
        if result["ext_color"] is None:
            # Stop capturing at common non-color words
            _COLOR_STOP = r'(?=\s+(?:over|with|and\s+\w+\s+leather|leather|paint|upholstery|interior|exterior|carbon|powered|featuring|,|\.))'
            patterns = [
                re.compile(r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,2})' + _COLOR_STOP, re.IGNORECASE),
                re.compile(r'(?:finished|painted|wrapped)\s+in\s+([A-Z][A-Za-z]+(?:[\s-]+[A-Za-z]+){0,2})', re.IGNORECASE),
                re.compile(r'(?:exterior\s+is|exterior\s+color\s*(?:is)?)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})', re.IGNORECASE),
            ]
            for pat in patterns:
                m = pat.search(description)
                if m:
                    result["ext_color"] = m.group(1).strip()
                    break

        # Interior color
        if result["int_color"] is None:
            patterns = [
                re.compile(r'over\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,3})\s+(?:leather|alcantara|suede|interior|upholstery)', re.IGNORECASE),
                re.compile(r'(?:interior\s+is|interior\s+in)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,3})', re.IGNORECASE),
            ]
            for pat in patterns:
                m = pat.search(description)
                if m:
                    result["int_color"] = m.group(1).strip()
                    break

        # Engine
        if result["engine_spec"] is None:
            m = re.search(
                r'((?:naturally[- ]aspirated|twin[- ]turbocharged|quad[- ]turbocharged|'
                r'supercharged|turbocharged)\s+)?'
                r'([\d.]+[- ](?:liter|litre)\s+(?:DOHC\s+|SOHC\s+|OHV\s+)?'
                r'(?:V\d+|W\d+|flat[- ]?\d+|inline[- ]?\d+|straight[- ]?\d+|[A-Z]\d+[A-Za-z]*))',
                description,
                re.IGNORECASE,
            )
            if m:
                engine = ((m.group(1) or "") + m.group(2)).strip()
                result["engine_spec"] = engine

        # Transmission
        if result["transmission_raw"] is None:
            m = re.search(
                r'(\w+[- ]speed\s+(?:manual|automatic|PDK|DCT|dual[- ]clutch|'
                r'sequential|tiptronic|S-tronic|SMG|F1|e-gear|ISR)'
                r'(?:\s+(?:transmission|transaxle|gearbox))?)',
                description,
                re.IGNORECASE,
            )
            if m:
                result["transmission_raw"] = m.group(1).strip()
