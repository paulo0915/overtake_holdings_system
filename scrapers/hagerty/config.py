"""Hagerty Marketplace GraphQL + site URLs."""

import os

HAGERTY_SITE_ORIGIN = "https://www.hagerty.com"
HAGERTY_MARKETPLACE_BASE = f"{HAGERTY_SITE_ORIGIN}/marketplace"
GRAPHQL_ENDPOINT = "https://customer-api-v2.hagerty.com/graphql"

# Public AppSync-style key shipped with the Next.js client (override via .env if rotated).
HAGERTY_GRAPHQL_API_KEY = os.getenv("HAGERTY_GRAPHQL_API_KEY", "da2-ffvpvgysavcdnh6v4srbxe5fw4")

# Matches the Marketplace search UI default page size.
RESULTS_PER_PAGE = 33
