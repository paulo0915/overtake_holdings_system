"""Base scraper interface — all source scrapers must implement this."""

from abc import ABC, abstractmethod
from pathlib import Path

from .vehicle_filter import VehicleFilter


class BaseScraper(ABC):
    """Abstract base class for all source scrapers."""

    # Subclass must set these
    SOURCE_NAME: str = ""    # e.g. "Bring a Trailer"
    SOURCE_DOMAIN: str = ""  # e.g. "bringatrailer.com"
    DATABASE: str = ""       # "auctions" or "classifieds"

    def __init__(
        self,
        min_price: int = 0,
        output_dir: str | Path = "output",
        max_concurrent: int = 4,
        max_pages: int = 0,
        vehicle_filter: VehicleFilter | None = None,
    ):
        self.min_price = min_price
        self.output_dir = Path(output_dir)
        self.max_concurrent = max_concurrent
        self.max_pages = max_pages
        self.vehicle_filter = vehicle_filter

    @abstractmethod
    async def run(self) -> list[dict]:
        """
        Run the scraper and return a list of normalized records.

        Each record must be a dict matching the schema columns
        defined in shared.schemas (AUCTION_COLUMNS or CLASSIFIEDS_COLUMNS).
        """
        ...
