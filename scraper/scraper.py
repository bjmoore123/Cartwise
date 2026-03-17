"""
scraper/scraper.py  (v8 - serverless)

Fetches all current deals for a Publix store via the
services.publix.com/api/v4/savings REST endpoint.

KEY: `publixstore` must be sent as a REQUEST HEADER, not a query param.
     - WeeklyAd deals (BOGOs, regular sales) require getSavingType=WeeklyAd
     - Digital coupons require getSavingType=AllDeals  
     Both are fetched and merged.
"""

import json
import urllib.request

from shared.deal_parser import parse_deal

_BASE_URL = (
    "https://services.publix.com/api/v4/savings"
    "?smImg=235&enImg=368&fallbackImg=false&isMobile=false"
    "&page=1&pageSize=0&includePersonalizedDeals=false"
    "&languageID=1&isWeb=true"
)

SAVINGS_URL_WEEKLY = _BASE_URL + "&getSavingType=WeeklyAd"
SAVINGS_URL_COUPONS = _BASE_URL + "&getSavingType=AllDeals"


def _fetch(url: str, store_id: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept":      "application/json, text/plain, */*",
            "Origin":      "https://www.publix.com",
            "Referer":     "https://www.publix.com/",
            "publixstore": str(store_id),
            "User-Agent":  (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_deals(store_id: str) -> list:
    """Fetch and merge Weekly Ad + Digital Coupon deals for a store.

    Architecture #2: uses shared.deal_parser.parse_deal — comprehensive BOGO
    regex replaces old substring-match logic; output shape is identical.
    """
    seen_ids = set()
    deals    = []

    # 1. Weekly Ad (BOGOs, regular sales, extra savings)
    try:
        raw_wa = _fetch(SAVINGS_URL_WEEKLY, store_id)
        for d in (raw_wa.get("Savings") or []):
            item = parse_deal(d)
            if item["id"] and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                deals.append(item)
    except Exception as e:
        print(f"  Warning: Weekly Ad fetch failed for store {store_id}: {e}")

    # 2. Digital Coupons (separate pool)
    try:
        raw_dc = _fetch(SAVINGS_URL_COUPONS, store_id)
        for d in (raw_dc.get("Savings") or []):
            item = parse_deal(d)
            if item["id"] and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                deals.append(item)
    except Exception as e:
        print(f"  Warning: Digital Coupon fetch failed for store {store_id}: {e}")

    return deals
