"""Vehicle filter engine - matches listings against markets_to_track_closely.xlsx."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

# Exclusion keywords for replica/kit car filtering (used by full system, not BaT-only)
EXCLUSION_KEYWORDS = [
    "replica", "kit car", "tribute", "recreation", "re-creation",
]


@dataclass
class FilterRule:
    make: str
    model: str
    exact_match: bool
    notes: str
    aliases: list[str] = field(default_factory=list)


class VehicleFilter:
    """Filters listings against the approved vehicle list."""

    def __init__(self, filter_file: str | Path):
        self.rules: list[FilterRule] = []
        self.all_models_makes: set[str] = set()  # makes where "All models" applies
        self._load_filter_file(filter_file)

    def _load_filter_file(self, path: str | Path) -> None:
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb["closely"]

        rows = list(ws.iter_rows(min_row=2, values_only=True))
        for row in rows:
            if not row or not row[0]:
                continue

            make = str(row[0]).strip()
            model = str(row[1]).strip() if row[1] else ""
            exact_match_raw = str(row[2]).strip().lower() if len(row) > 2 and row[2] else ""
            exact_match = exact_match_raw == "yes"
            notes = str(row[3]).strip() if len(row) > 3 and row[3] else ""

            # Normalize make
            make = self._normalize_make(make)

            # Check for "All models"
            if model.lower() in ("all models", "all models"):
                self.all_models_makes.add(make.lower())
                continue

            # Handle numeric models (575.0 -> "575", 959.0 -> "959")
            try:
                model_float = float(model)
                if model_float == int(model_float):
                    model = str(int(model_float))
            except (ValueError, TypeError):
                pass

            # Build aliases from notes
            aliases = self._extract_aliases(make, model, notes)

            rule = FilterRule(
                make=make,
                model=model,
                exact_match=exact_match,
                notes=notes,
                aliases=aliases,
            )
            self.rules.append(rule)

        wb.close()

    def _normalize_make(self, make: str) -> str:
        """Normalize make names for consistent matching."""
        make = make.strip()
        # Standardize known variants
        make_lower = make.lower()
        if make_lower == "mercedes-amg":
            return "Mercedes-AMG"
        if make_lower == "mercedes-benz":
            return "Mercedes-Benz"
        return make

    def _extract_aliases(self, make: str, model: str, notes: str) -> list[str]:
        """Extract model aliases from notes column."""
        aliases = []
        make_lower = make.lower()

        # Ferrari-specific aliases
        if make_lower == "ferrari":
            if model == "430 Scuderia":
                aliases.append("F430 Scuderia")
            elif model == "Scuderia Spider 16M":
                aliases.extend([
                    "430 Scuderia Spider 16M",
                    "F430 Scuderia Spider 16M",
                ])
            elif model == "F430":
                aliases.append("430")

        # Lamborghini Reventon accent handling
        if make_lower == "lamborghini" and "revent" in model.lower():
            aliases.append("Reventon")
            aliases.append("Reventón")

        # Honda S2000 CR / Club Racer
        if make_lower == "honda" and model == "S2000 CR":
            aliases.append("S2000 Club Racer")

        # Mercedes 190E evo
        if make_lower == "mercedes-benz" and "190e" in model.lower():
            if "evo i" in model.lower():
                aliases.append("190E Evolution I")
                aliases.append("190E Evo I")
            elif "evo ii" in model.lower():
                aliases.append("190E Evolution II")
                aliases.append("190E Evo II")

        # Nissan Skyline GTR
        if make_lower == "nissan" and "skyline" in model.lower():
            aliases.append("Skyline GT-R")
            aliases.append("Skyline GTR")

        return aliases

    def _check_exclusions(self, title: str, excerpt: str = "") -> bool:
        """Return True if listing should be excluded."""
        combined = f"{title} {excerpt}".lower()
        for kw in EXCLUSION_KEYWORDS:
            if kw in combined:
                # Don't exclude legitimate factory continuation cars
                if "singer" in combined or "gma" in combined or "t.50" in combined:
                    return False
                return True
        return False

    def _title_contains_porsche_chassis_code(self, title: str) -> bool:
        """Check if title contains Porsche chassis codes instead of '911'."""
        codes = ["930", "964", "993", "996", "997", "991", "992"]
        title_lower = title.lower()
        for code in codes:
            # Match standalone code (not part of a larger number)
            if re.search(rf'\b{code}\b', title_lower):
                return True
        return False

    def _model_matches(self, rule: FilterRule, title: str, make_in_title: str) -> bool:
        """Check if a listing title matches a model rule."""
        # Remove the make from title for model matching
        title_for_model = title

        model_lower = rule.model.lower()
        title_lower = title_for_model.lower()

        # For Porsche 911 models, also accept chassis codes
        if rule.make.lower() == "porsche" and "911" in model_lower:
            # Extract the model-specific part after "911"
            model_suffix = model_lower.replace("911", "").strip()

            # Check direct "911" match
            if "911" in title_lower:
                if model_suffix:
                    if rule.exact_match:
                        return self._exact_model_match(model_suffix, title_lower, "911")
                    else:
                        return self._fuzzy_model_match(model_suffix, title_lower)
                return True

            # Check chassis code match
            if self._title_contains_porsche_chassis_code(title):
                if model_suffix:
                    if rule.exact_match:
                        return self._exact_chassis_model_match(model_suffix, title_lower)
                    else:
                        return self._fuzzy_model_match(model_suffix, title_lower)
                return True

            return False

        # Check aliases first
        for alias in rule.aliases:
            alias_lower = alias.lower()
            if alias_lower in title_lower:
                if rule.exact_match:
                    return self._exact_alias_match(alias_lower, title_lower, make_in_title)
                return True

        # Standard model matching
        if rule.exact_match:
            return self._exact_model_match(model_lower, title_lower, make_in_title)
        else:
            return self._fuzzy_model_match(model_lower, title_lower)

    def _fuzzy_model_match(self, model_lower: str, title_lower: str) -> bool:
        """All words in model must appear in title."""
        model_words = model_lower.split()
        return all(word in title_lower for word in model_words)

    def _exact_model_match(self, model_lower: str, title_lower: str, make_in_title: str = "") -> bool:
        """The model string must appear exactly, with no extra model words."""
        # Check if the exact model phrase is in the title
        if model_lower not in title_lower:
            return False

        # For exact match, find the model in the title and ensure no extra
        # variant words follow. We use word boundary approach.
        pattern = re.escape(model_lower) + r'(?:\s|$|[,.])'
        return bool(re.search(pattern, title_lower))

    def _exact_alias_match(self, alias_lower: str, title_lower: str, make_in_title: str = "") -> bool:
        """Exact match for an alias."""
        pattern = re.escape(alias_lower) + r'(?:\s|$|[,.])'
        return bool(re.search(pattern, title_lower))

    def _exact_chassis_model_match(self, model_suffix: str, title_lower: str) -> bool:
        """Exact match when using chassis codes for Porsche."""
        suffix_words = model_suffix.split()
        return all(word in title_lower for word in suffix_words)

    def match(self, title: str, excerpt: str = "") -> tuple[bool, str, str] | None:
        """
        Check if a listing matches the vehicle filter.

        Returns (matched, make, model) if matched, None if not.
        """
        if self._check_exclusions(title, excerpt):
            return None

        title_clean = title.strip()

        # Bertone remapping
        if "bertone" in title_clean.lower():
            title_clean = re.sub(r'(?i)\bbertone\b', 'Lamborghini', title_clean)

        # Check "All models" makes first
        for make in self.all_models_makes:
            if make in title_clean.lower():
                # Extract model from title (everything after make)
                model = self._extract_model_from_title(title_clean, make)
                return (True, make.title() if make != "ferrari" else self._proper_make(make), model)

        # Check specific model rules
        for rule in self.rules:
            make_lower = rule.make.lower()

            # Check if make is in title
            # Handle multi-word makes
            if make_lower not in title_clean.lower():
                # Also check without hyphens
                if make_lower.replace("-", " ") not in title_clean.lower().replace("-", " "):
                    continue

            if self._model_matches(rule, title_clean, rule.make):
                return (True, rule.make, rule.model)

        return None

    def _proper_make(self, make_lower: str) -> str:
        """Return properly cased make name."""
        make_map = {
            "ferrari": "Ferrari",
            "bugatti": "Bugatti",
            "koenigsegg": "Koenigsegg",
            "gordon murray automotive": "Gordon Murray Automotive",
            "lamborghini": "Lamborghini",
            "porsche": "Porsche",
            "mclaren": "McLaren",
            "mercedes-benz": "Mercedes-Benz",
            "mercedes-amg": "Mercedes-AMG",
            "aston martin": "Aston Martin",
            "bmw": "BMW",
            "audi": "Audi",
            "honda": "Honda",
            "ford": "Ford",
            "nissan": "Nissan",
            "pagani": "Pagani",
            "lexus": "Lexus",
            "acura": "Acura",
            "lancia": "Lancia",
        }
        return make_map.get(make_lower, make_lower.title())

    def _extract_model_from_title(self, title: str, make_lower: str) -> str:
        """Extract model name from title for 'All models' makes."""
        title_lower = title.lower()
        idx = title_lower.find(make_lower)
        if idx == -1:
            return ""

        # Get everything after the make name
        after_make = title[idx + len(make_lower):].strip()
        # The model is typically the rest of the title
        return after_make
