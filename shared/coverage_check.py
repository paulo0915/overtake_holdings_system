"""Per-source field-coverage gates and row-count floors.

Catches silent regressions: if a previously-populated field drops to ~0%
(typical symptom of upstream HTML/JSON shape change) or row count collapses,
the validator raises ``CoverageRegression`` instead of letting a broken
scrape silently overwrite good data downstream.

State files live under ``assets/``:

* ``coverage_baseline.json`` — last-known-good fill % per (source, field).
  Updated automatically on every passing run.
* ``source_floors.json`` — hand-tuned minimum row counts per source.

Both are JSON so they diff cleanly in git.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
BASELINE_PATH = ROOT / "assets" / "coverage_baseline.json"
FLOORS_PATH = ROOT / "assets" / "source_floors.json"

# Relative drop below which a previously-populated field counts as a regression.
# Example: a field at 90% baseline that drops below 90 * (1 - 0.4) = 54% trips.
RELATIVE_DROP_THRESHOLD = 0.40

# Absolute floor: a field below baseline that hits this value is always a fail
# even if the relative drop is small (catches "98% -> 50%" cases on noisy fields).
ABSOLUTE_FLOOR = 0.20

# A field must have at least this baseline coverage to be guarded. Below this,
# the field is too sparse to reason about and we ignore it.
GUARD_MIN_BASELINE = 0.50

# Fields whose absence is never a scraper bug — don't guard them.
_UNGUARDED_FIELDS = frozenset({
    "Record_ID", "Scraped_at", "Source", "Auction_house",
    "Last_price_change_date", "Previous_asking_price",
    "Estimate_low", "Estimate_high", "Estimate_currency",
    "Auction_event",
    "FX_rate_used",
})


class CoverageRegression(Exception):
    """Raised when a scraped batch fails its coverage / row-count gate."""


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not parse %s — treating as empty", path)
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def compute_coverage(records: Iterable[dict], columns: Iterable[str]) -> dict[str, float]:
    """Return ``{field: fill_ratio}`` over the given records (0.0–1.0)."""
    rows = list(records)
    n = len(rows)
    if n == 0:
        return {c: 0.0 for c in columns}
    out: dict[str, float] = {}
    for c in columns:
        if c in _UNGUARDED_FIELDS:
            continue
        filled = sum(
            1 for r in rows
            if r.get(c) not in (None, "", [], {}) and str(r.get(c)).strip()
        )
        out[c] = filled / n
    return out


def check_and_update_baseline(
    *,
    source: str,
    records: list[dict],
    columns: list[str],
    update_baseline: bool = True,
) -> None:
    """Validate coverage + row count for one source. Raises on regression.

    On success and ``update_baseline=True``, the baseline file is rewritten
    with the new (typically equal-or-better) coverage so the next run gates
    against the freshest acceptable shape.
    """
    floors = _load_json(FLOORS_PATH)
    baselines = _load_json(BASELINE_PATH)

    n = len(records)
    floor = floors.get(source, 0)
    if n < floor:
        raise CoverageRegression(
            f"[{source}] row-count regression: got {n}, floor is {floor}. "
            f"Likely upstream block, schema change, or filter misconfig."
        )

    if n == 0:
        # Floor was 0 (or missing) AND we got zero rows. Nothing to compare;
        # log a warning so it shows up in the run summary.
        logger.warning("[%s] returned 0 records and has no row-count floor set", source)
        return

    new_cov = compute_coverage(records, columns)
    prior_cov = baselines.get(source, {})

    regressions: list[str] = []
    for field, prior_ratio in prior_cov.items():
        if prior_ratio < GUARD_MIN_BASELINE:
            continue
        new_ratio = new_cov.get(field, 0.0)
        rel_drop = (prior_ratio - new_ratio) / prior_ratio
        if rel_drop >= RELATIVE_DROP_THRESHOLD and new_ratio <= prior_ratio - 0.10:
            regressions.append(
                f"  {field}: {prior_ratio:.0%} -> {new_ratio:.0%} "
                f"(rel drop {rel_drop:.0%})"
            )
        elif new_ratio <= ABSOLUTE_FLOOR and prior_ratio >= 0.80:
            regressions.append(
                f"  {field}: {prior_ratio:.0%} -> {new_ratio:.0%} "
                f"(below absolute floor)"
            )

    if regressions:
        msg = (
            f"[{source}] field-coverage regression vs baseline:\n"
            + "\n".join(regressions)
            + "\n  -> Likely upstream HTML/API shape change. Investigate before publishing."
        )
        raise CoverageRegression(msg)

    if update_baseline:
        # Merge: keep prior fields not present in this run (don't lose history),
        # take the max(prior, new) so a single bad run can't permanently lower
        # the baseline. Coverage only ratchets up.
        merged = dict(prior_cov)
        for k, v in new_cov.items():
            merged[k] = max(merged.get(k, 0.0), v)
        baselines[source] = merged
        _save_json(BASELINE_PATH, baselines)
        logger.debug("[%s] baseline updated: %d fields tracked", source, len(merged))
