"""FX conversion using Frankfurter API."""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import FRANKFURTER_API_BASE

logger = logging.getLogger(__name__)


class FXConverter:
    """Convert currencies to USD using historical exchange rates."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._cache: dict[tuple[str, str], float] = {}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
    )
    async def _fetch_rate(self, date_str: str, from_currency: str) -> float:
        """Fetch exchange rate from Frankfurter API."""
        cache_key = (date_str, from_currency.upper())
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{FRANKFURTER_API_BASE}/{date_str}"
        params = {"from": from_currency.upper(), "to": "USD"}

        resp = await self.client.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rate = data.get("rates", {}).get("USD")
        if rate is None:
            raise ValueError(f"No USD rate found for {from_currency} on {date_str}")

        self._cache[cache_key] = rate
        return rate

    async def get_rate(self, date_str: str, from_currency: str) -> float:
        """
        Get exchange rate to USD for a given date and currency.

        If the exact date is a weekend/holiday, Frankfurter returns the
        closest previous business day rate.

        If ``date_str`` is in the future (e.g. mis-set ``dateEnd`` on a listing),
        it is clamped to today's UTC date — ECB/Frankfurter has no future rates.

        Returns 1.0 if currency is already USD.
        """
        if from_currency.upper() == "USD":
            return 1.0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if date_str > today:
            logger.debug(
                "FX date %s is in the future; using %s for Frankfurter lookup",
                date_str,
                today,
            )
            date_str = today

        try:
            return await self._fetch_rate(date_str, from_currency)
        except Exception as e:
            logger.warning(f"FX lookup failed for {from_currency} on {date_str}: {e}")
            # Try previous day as fallback
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                prev_day = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
                return await self._fetch_rate(prev_day, from_currency)
            except Exception:
                logger.error(f"FX fallback also failed for {from_currency} on {date_str}")
                return float("nan")

    async def convert_to_usd(
        self, amount: int | float | None, currency: str, date_str: str | None
    ) -> tuple[float | None, float | None]:
        """
        Convert amount to USD.

        Returns (amount_usd, fx_rate_used).
        Returns (None, None) if amount is None.
        """
        if amount is None:
            return (None, None)

        if not date_str:
            logger.warning("No date for FX conversion, using today")
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rate = await self.get_rate(date_str, currency)

        if rate != rate:  # NaN check
            return (None, None)

        amount_usd = round(amount * rate, 2)
        return (amount_usd, round(rate, 6))
