"""C&B record assembly loop — dedup, vehicle filter, delegate to listing_parser."""

import logging

from . import listing_parser

logger = logging.getLogger(__name__)


def build_records(
    listings: list[dict],
    details: dict[str, dict],
    vehicle_filter,
    scraped_at: str,
) -> list[dict]:
    """
    Assemble all C&B auction records with dedup and vehicle filter applied.

    Details dict must be keyed by str(auction_id) to match listing IDs.
    """
    records = []
    seen_ids: set[str] = set()
    skipped = {"filter": 0, "dup": 0, "error": 0}

    for item in listings:
        auction_id = str(item.get("id", ""))

        if auction_id in seen_ids:
            skipped["dup"] += 1
            continue
        seen_ids.add(auction_id)

        detail = details.get(auction_id, {})
        if not detail:
            skipped["error"] += 1
            continue

        record = listing_parser.build_record(item, detail, scraped_at)
        if not record:
            skipped["error"] += 1
            continue

        if vehicle_filter:
            if not vehicle_filter.match(record.get("Title", ""), ""):
                skipped["filter"] += 1
                continue

        records.append(record)

    for k, v in skipped.items():
        if v:
            logger.info(f"Skipped {v} ({k})")

    return records
