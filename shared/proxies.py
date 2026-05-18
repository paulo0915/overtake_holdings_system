"""Residential-proxy rotation via iProyal env-based credentials.

Set in ``.env``:
    IPROYAL_USERNAME=...
    IPROYAL_PASSWORD=...

Each call to ``iter_available_proxies()`` / ``get_random_proxy()`` builds a
fresh proxy URL from the credentials plus a randomly-picked port + country
code. iProyal's rotating endpoint serves a different residential IP per
request, so individual URLs don't need to be tracked in a DB and
``mark_bad()`` is a no-op.

URL shape (built on the fly):
    geo.iproyal.com:{PORT}:{USER}:{PASS}_country-{COUNTRY}

PORT pool covers iProyal's documented rotating-port ranges (11200-11250
and 51200-51250). COUNTRY pool is the Akamai-friendly subset proven to
pass autotrader.com / je.com IP-reputation checks.

Scrapers that need IP-level bypass (AutoTrader.com / JE / TrueCar) call
``get_random_proxy()`` to grab one fresh URL and feed it to both their
Playwright context and curl_cffi session.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from .config import IPROYAL_HOST, IPROYAL_PASSWORD, IPROYAL_USERNAME

logger = logging.getLogger(__name__)

# iProyal rotating-endpoint ports. Within each range, every port maps to
# the same residential pool but a different rotation policy slot — using
# multiple ports decorrelates concurrent requests from the same vendor.
_IPROYAL_PORTS: tuple[int, ...] = tuple(
    list(range(11200, 11251)) + list(range(51200, 51251))
)

# Countries whose iProyal residential IPs empirically pass Akamai's
# reputation check on US car-classified sites. Ranked best-first based
# on prior probe data; randomized at pick time so no single country
# carries the whole pool's burn risk.
_IPROYAL_COUNTRIES_DEFAULT: tuple[str, ...] = (
    # Asia (cleanest, smallest scraper market)
    "jp", "tw", "kr",
    # Nordic (premium ISPs, low scraper density)
    "se", "no", "fi", "dk",
    # Baltic (small market, premium ISPs)
    "ee", "lt", "lv",
    # Central / Eastern Europe (low scraper burn)
    "cz", "sk", "hu", "bg", "si",
    # Western Europe outside DE/FR/UK burn cluster
    "at", "ch", "pt",
)


@dataclass(frozen=True)
class ProxyInfo:
    """A residential proxy parsed into Playwright- and curl_cffi-friendly forms.

    ``url`` is the raw ``http://user:pass@host:port`` string (what curl_cffi
    and httpx both accept directly). ``playwright_dict`` is the shape
    Playwright's ``new_context(proxy=...)`` expects, with auth split off
    because Chromium's ``--proxy-server`` launch flag doesn't parse userinfo.
    """
    url: str
    server: str
    username: str | None
    password: str | None

    @property
    def playwright_dict(self) -> dict[str, str]:
        """Shape Playwright wants: ``{server, username?, password?}``."""
        d: dict[str, str] = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        return d


def _parse(proxy: str) -> ProxyInfo:
    """Normalise a stored proxy string into Playwright / curl_cffi forms.

    Accepts two shapes:

        http://user:pass@host:port       (standard URL)
        host:port:user:pass              (iProyal / Smartproxy shorthand)

    The shorthand form is common because residential-proxy vendors ship
    plaintext lists in that layout. We split on the **first three** colons
    so passwords that contain literal ``:`` survive intact.

    Returns ``ProxyInfo`` with ``url`` always in standard URL form (that's
    what curl_cffi and httpx consume natively). Raises ``ValueError`` on
    anything we can't parse — we'd rather abort than silently connect
    without the proxy we thought we had.
    """
    proxy = proxy.strip()
    if not proxy:
        raise ValueError("empty proxy string")

    if "://" in proxy:
        return _parse_url_form(proxy)
    return _parse_shorthand(proxy)


def _parse_url_form(url: str) -> ProxyInfo:
    p = urlparse(url)
    if not p.scheme or not p.hostname or not p.port:
        raise ValueError(
            f"proxy URL must be ``scheme://[user:pass@]host:port``, got {url!r}"
        )
    server = f"{p.scheme}://{p.hostname}:{p.port}"
    return ProxyInfo(
        url=url,
        server=server,
        username=p.username,
        password=p.password,
    )


def _parse_shorthand(raw: str) -> ProxyInfo:
    """``host:port:user:pass`` → ``ProxyInfo``. Scheme defaults to ``http``."""
    # maxsplit=3 so the password can contain its own colons (some providers
    # stuff session tokens in there).
    parts = raw.split(":", 3)
    if len(parts) < 2:
        raise ValueError(
            f"proxy shorthand must be ``host:port[:user:pass]``, got {raw!r}"
        )
    host, port = parts[0].strip(), parts[1].strip()
    if not host or not port.isdigit():
        raise ValueError(f"invalid host/port in {raw!r}")

    username = parts[2] if len(parts) > 2 else None
    password = parts[3] if len(parts) > 3 else None

    server = f"http://{host}:{port}"
    if username and password:
        # URL-encode userinfo so vendor-assigned passwords with ``@`` / ``#``
        # / etc. don't break the URL form that curl_cffi consumes.
        safe_user = quote(username, safe="")
        safe_pass = quote(password, safe="")
        url = f"http://{safe_user}:{safe_pass}@{host}:{port}"
    elif username:
        safe_user = quote(username, safe="")
        url = f"http://{safe_user}@{host}:{port}"
    else:
        url = server

    return ProxyInfo(
        url=url,
        server=server,
        username=username,
        password=password,
    )


def _redact(url: str) -> str:
    """Hide credentials in logs — keep only scheme + host + port."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}:{p.port}"
    except Exception:
        return "<proxy>"


def _build_iproyal_url(port: int, country: str) -> str:
    """Compose an iProyal rotating proxy shorthand from env credentials.

    Output shape: ``geo.iproyal.com:{port}:{user}:{pass}_country-{cc}``
    The password gets a ``_country-<cc>`` suffix that iProyal parses
    on its side to scope the rotation pool to that country.
    """
    cc = country.strip().lower()
    pwd = f"{IPROYAL_PASSWORD}_country-{cc}" if IPROYAL_PASSWORD else f"_country-{cc}"
    return f"{IPROYAL_HOST}:{port}:{IPROYAL_USERNAME}:{pwd}"


def iter_available_proxies(
    limit: int = 3,
    country_allow: list[str] | None = None,
) -> list[ProxyInfo]:
    """Generate ``limit`` fresh iProyal rotating proxies.

    Each entry is built on the fly from ``IPROYAL_USERNAME`` / ``IPROYAL_PASSWORD``
    + a random (port, country) pick. iProyal's rotating endpoint serves a
    different residential IP per request, so callers don't need to track
    which entries they've used — every iter call produces uniquely-routed
    proxies.

    ``country_allow`` narrows the country pool (e.g. AutoTrader's
    Akamai-friendly subset). When ``None`` we use the project's default
    Akamai-friendly list. Unknown country codes are dropped silently.

    Returns ``[]`` if IPROYAL credentials aren't configured.
    """
    if not (IPROYAL_USERNAME and IPROYAL_PASSWORD):
        logger.error(
            "iProyal credentials missing — set IPROYAL_USERNAME / "
            "IPROYAL_PASSWORD in .env to use rotating residential proxies.",
        )
        return []

    countries: tuple[str, ...]
    if country_allow:
        # Keep only countries also in the Akamai-friendly default list, in
        # the caller's preferred order. Falls back to the full pool if the
        # caller's list has no overlap (better than returning nothing).
        wanted = tuple(c.lower() for c in country_allow)
        intersection = tuple(c for c in wanted if c in _IPROYAL_COUNTRIES_DEFAULT)
        countries = intersection or _IPROYAL_COUNTRIES_DEFAULT
    else:
        countries = _IPROYAL_COUNTRIES_DEFAULT

    out: list[ProxyInfo] = []
    for _ in range(max(0, limit)):
        port = random.choice(_IPROYAL_PORTS)
        country = random.choice(countries)
        try:
            out.append(_parse_shorthand(_build_iproyal_url(port, country)))
        except ValueError as e:
            logger.warning("Skipping malformed generated proxy: %s", e)
    return out


def get_random_proxy() -> ProxyInfo | None:
    """Generate one fresh iProyal rotating proxy. ``None`` if creds unset."""
    proxies = iter_available_proxies(limit=1)
    return proxies[0] if proxies else None


def mark_bad(proxy_url: str) -> None:
    """No-op for the env-generated rotating-proxy backend.

    iProyal rotates IPs per request, so a single URL never identifies
    a persistent "bad" endpoint — every call hits a different IP. We
    keep the function so existing callers don't need to be touched;
    they can still call it after a failed warmup. Logged at DEBUG level
    so it's visible when troubleshooting but doesn't spam at INFO.
    """
    logger.debug(
        "mark_bad called on rotating-proxy URL (no-op): %s",
        _redact(proxy_url),
    )
