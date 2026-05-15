"""Residential-proxy rotation against the Supabase ``proxies`` table.

See ``db/migrations/003_create_proxies.sql`` for the schema.

Proxy row format: whatever the provider gives you. Both of these are
accepted and normalised at import time:

    host:port:user:pass                 (iProyal / Smartproxy shorthand)
    http://user:pass@host:port          (standard URL form)

Storing the shorthand as-is is convenient because you can paste dealer-
provided proxy lists straight into the SQL editor without reformatting.

Scrapers that need to bypass IP-level bot protection (AutoTrader.com /
TrueCar / etc.) call ``get_random_proxy()`` to grab a non-bad entry, then
feed it to both their Playwright context and curl_cffi session. On a
block, they call ``mark_bad()`` and retry with the next one.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from .db import get_client

logger = logging.getLogger(__name__)


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


def get_random_proxy() -> ProxyInfo | None:
    """Pick a non-bad proxy at random. Returns ``None`` if the table is
    missing / empty / every proxy has been flagged.
    """
    candidates = iter_available_proxies(limit=1000)
    if not candidates:
        return None
    return random.choice(candidates)


def mark_bad(proxy_url: str) -> None:
    """Flip ``is_bad=true`` for the given proxy URL. Idempotent — safe to
    call repeatedly on the same value.

    Proxies are stored in shorthand form (``host:port:user:pass``) but
    ``mark_bad`` receives a URL (``http://user:pass@host:port``). We try
    both the URL form and the reconstructed shorthand so the WHERE clause
    matches regardless of how the row was originally inserted.
    """
    client = get_client()
    updated = (
        client.table("proxies")
        .update({"is_bad": True})
        .eq("proxy", proxy_url)
        .execute()
    )
    if not (updated.data or []):
        # URL form didn't match — reconstruct shorthand and retry.
        try:
            from urllib.parse import urlparse
            p = urlparse(proxy_url)
            if p.hostname and p.port and p.username and p.password:
                shorthand = f"{p.hostname}:{p.port}:{p.username}:{p.password}"
                client.table("proxies").update({"is_bad": True}).eq("proxy", shorthand).execute()
        except Exception:
            pass
    logger.info("Marked proxy as bad: %s", _redact(proxy_url))


def _redact(url: str) -> str:
    """Hide credentials in logs — keep only scheme + host + port."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}:{p.port}"
    except Exception:
        return "<proxy>"


def iter_available_proxies(
    limit: int = 3,
    country_allow: list[str] | None = None,
) -> list[ProxyInfo]:
    """Return up to ``limit`` non-bad proxies in random order.

    ``country_allow`` (optional) filters to proxies whose stored string
    contains ``country-<cc>`` — iProyal / Smartproxy / most residential
    providers embed the country code in either the password or a subdomain.
    Use for scrapers where only certain country pools pass the target's
    IP reputation checks (e.g. AutoTrader.com passes JP/PL but hard-blocks
    US/AR residentials).

    Returns ``[]`` (not raise) when the ``proxies`` table doesn't exist
    yet — the scraper's error message then tells the user what to run.
    """
    client = get_client()
    # PostgREST caps a single SELECT at 1000 rows by default. With a 5k+
    # proxy pool we have to page through ``.range()`` to see every
    # country slice, otherwise scrapers always get the same first-1000
    # bias and the country_allow filter mismatches what's actually in
    # the DB.
    PAGE = 1000
    rows: list[dict] = []
    start = 0
    try:
        while True:
            end = start + PAGE - 1
            batch = (
                client.table("proxies")
                .select("proxy")
                .eq("is_bad", False)
                .range(start, end)
                .execute()
            )
            page_rows = batch.data or []
            if not page_rows:
                break
            rows.extend(page_rows)
            if len(page_rows) < PAGE:
                break
            start += PAGE
    except Exception as e:
        logger.error(
            "Failed to query 'proxies' table (does it exist?): %s", e,
        )
        return []

    if country_allow:
        wanted = tuple(f"country-{cc.lower()}" for cc in country_allow)
        rows = [r for r in rows if any(tok in r["proxy"].lower() for tok in wanted)]

    random.shuffle(rows)
    out: list[ProxyInfo] = []
    for row in rows[:limit]:
        try:
            out.append(_parse(row["proxy"]))
        except ValueError as e:
            logger.warning("Skipping malformed proxy: %s", e)
    return out
