"""Debug Barrett-Jackson API entry for a specific lot."""
import asyncio, sys, json
sys.path.insert(0, ".")
import httpx
from shared.config import get_headers
from scrapers.event.barrett_jackson.config import SITE_ORIGIN, RESULTS_API_PATH
from urllib.parse import urlencode

# The lot: 1998 Ferrari F300 (lot_id 161324), scottsdale-2014
# slug from URL: 1998-ferrari-f300-161324
TARGET_SLUG = "1998-ferrari-f300-161324"

async def main():
    params = {
        "page": "1", "type": "Vehicles", "size": "50",
        "slug": "scottsdale-2014", "q": TARGET_SLUG,
        "sort": "price_desc", "make": "", "year": "", "is_reserve": "",
        "collection": "", "style": "", "dayOfWeek": "", "lot": "",
        "model": "", "engine_size": "", "interior_color": "",
        "exterior_color": "", "transmission_type_name": "",
        "number_of_cylinders": "", "nonIncludedSlugs": "",
    }
    url = f"{SITE_ORIGIN}{RESULTS_API_PATH}?{urlencode(params)}"
    headers = get_headers(accept="application/json", referer=f"{SITE_ORIGIN}/results?type=Vehicles")
    headers["Origin"] = SITE_ORIGIN

    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        r = await client.get(url, headers=headers)
        print("HTTP", r.status_code)
        data = r.json()
        entries = data.get("data") or []
        print(f"Entries returned: {len(entries)}")
        for e in entries:
            attrs = e.get("attributes", {})
            slug = attrs.get("slug", "")
            if TARGET_SLUG in slug or "f300" in slug.lower() or "161324" in slug:
                print("\n=== MATCH ===")
                print("vin:", repr(attrs.get("vin")))
                print("VIN:", repr(attrs.get("VIN")))
                print("slug:", repr(slug))
                print("title:", repr(attrs.get("title")))
                # Show all attrs keys with non-empty values
                for k, v in attrs.items():
                    if v and k not in ("short_description", "full_description", "main_image_url"):
                        print(f"  {k}: {repr(str(v)[:80])}")

asyncio.run(main())
