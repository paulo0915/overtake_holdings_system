"""Broad Arrow Auctions — session-aware, event-based scraping strategy.

Discovery flow (matches a real browser navigation, which satisfies reCAPTCHA v3 scoring):

    GET /                          — warms session / sets cookies
    GET /past-auctions             — lists every auction event URL
    GET /events/event/{name}       — each event page embeds a branch-specific
                                     /vehicles/results?q[branch_id_eq]=N URL
    GET /vehicles/results?q[branch_id_eq]=N&page=K  — vehicle card listing
    GET /vehicles/{lot}/{slug}     — detail page (JSON-LD Car schema)

Note: the "all-branches" URL the browser's Results filter builds
(many q[branch_id_in][] params) is NOT used here. Instead, we derive each
branch ID from the event page and only rely on globally-listed event pages,
so new auctions are picked up automatically without config changes.
"""

SITE_ORIGIN = "https://www.broadarrowauctions.com"
PAST_AUCTIONS_PATH = "/past-auctions"
EVENTS_BASE_PATH = "/events/event"
RESULTS_PATH = "/vehicles/results"
SORT_PARAM = "q[s][0][name_dir]"
SORT_VALUE = "sales_price.desc"
