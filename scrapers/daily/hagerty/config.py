"""Hagerty Marketplace GraphQL + site URLs."""

HAGERTY_SITE_ORIGIN = "https://www.hagerty.com"
HAGERTY_MARKETPLACE_BASE = f"{HAGERTY_SITE_ORIGIN}/marketplace"
GRAPHQL_ENDPOINT = "https://customer-api-v2.hagerty.com/graphql"

# Last-resort if auto-discovery and ``HAGERTY_GRAPHQL_API_KEY`` env are both unavailable.
HAGERTY_GRAPHQL_API_KEY_FALLBACK = "da2-ffvpvgysavcdnh6v4srbxe5fw4"

# Matches the Marketplace search UI default page size.
RESULTS_PER_PAGE = 33
