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
    """Fetch and merge Weekly Ad + Digital Coupon deals for a store (list only)."""
    return get_deals_with_status(store_id)[0]


def get_deals_with_status(store_id: str) -> tuple:
    """Fetch and merge Weekly Ad + Digital Coupon deals for a store.

    Returns (deals, failed_sources). v9.2.2 (#6): callers need to know when a
    source failed, so a Publix outage never replaces a good cache with a
    partial or empty one.

    Architecture #2: uses shared.deal_parser.parse_deal — comprehensive BOGO
    regex replaces old substring-match logic; output shape is identical.
    """
    seen_ids = set()
    deals    = []
    failed   = []

    # 1. Weekly Ad (BOGOs, regular sales, extra savings)
    try:
        raw_wa = _fetch(SAVINGS_URL_WEEKLY, store_id)
        for d in (raw_wa.get("Savings") or []):
            item = parse_deal(d)
            if item["id"] and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                deals.append(item)
    except Exception as e:
        failed.append("weekly_ad")
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
        failed.append("digital_coupons")
        print(f"  Warning: Digital Coupon fetch failed for store {store_id}: {e}")

    return deals, failed
