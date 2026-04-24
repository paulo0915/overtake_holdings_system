-- Residential-proxy pool for scrapers that need to bypass IP-level bot
-- protection (AutoTrader.com, TrueCar, etc.).
--
-- Usage from Python: see ``shared/proxies.py``
--   - ``get_random_proxy()`` picks a non-bad proxy at random
--   - ``iter_available_proxies(limit)`` returns up to N in random order
--   - ``mark_bad(proxy_url)`` flips the ``is_bad`` flag when a proxy fails
--
-- Either of these row formats is accepted — ``shared/proxies.py::_parse``
-- normalises both at import time:
--
--     host:port:user:pass             (iProyal / Smartproxy shorthand)
--     http://user:pass@host:port      (standard URL)
--
-- Paste vendor-shipped lists directly; the codebase handles the rest.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS proxies (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proxy   TEXT NOT NULL UNIQUE,
    is_bad  BOOLEAN NOT NULL DEFAULT FALSE
);

-- Index used by ``iter_available_proxies`` (filter on ``is_bad = FALSE``).
CREATE INDEX IF NOT EXISTS idx_proxies_is_bad ON proxies(is_bad);

-- Seed example (commented out — uncomment + replace before running):
-- INSERT INTO proxies (proxy) VALUES
--     ('http://user:pass@proxy1.provider.com:8080'),
--     ('http://user:pass@proxy2.provider.com:8080')
-- ON CONFLICT (proxy) DO NOTHING;
