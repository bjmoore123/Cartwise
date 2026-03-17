"""lambda/deals.py — Store search and deal retrieval.

Architecture #2: uses shared.deal_parser.parse_deal for unified BOGO/field parsing.
PDC-16: search_stores requires session auth.
"""
import os
import json
import re
import urllib.request
import urllib.parse

from helpers import (
    ok, err, _to_py, get_session,
    dynamodb, history_tbl, corpus_tbl, users_table,
    FRONTEND_URL, API_URL, DEFAULT_PREFS,
)
from logging_utils import _log_app_event
from shared.deal_parser import parse_deal
from shared.matcher import match_deals_for_user

# ── Publix API URLs ───────────────────────────────────────────────────────────

_SAVINGS_BASE = (
    "https://services.publix.com/api/v4/savings"
    "?smImg=235&enImg=368&fallbackImg=false&isMobile=false"
    "&page=1&pageSize=0&includePersonalizedDeals=false"
    "&languageID=1&isWeb=true"
)
SAVINGS_URL_WEEKLY  = _SAVINGS_BASE + "&getSavingType=WeeklyAd"
SAVINGS_URL_COUPONS = _SAVINGS_BASE + "&getSavingType=AllDeals"

# Headers required by Publix storelocator API (CORS-protected)
_STORE_HDRS = {
    "Accept":          "application/geo+json",
    "Accept-Encoding": "gzip, deflate",
    "Origin":          "https://www.publix.com",
    "Referer":         "https://www.publix.com/",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ── Store search ──────────────────────────────────────────────────────────────

def _parse_store_feature(f: dict) -> dict:
    """Parse a GeoJSON feature from the storelocator API into a clean store dict."""
    p      = f.get("properties") or f
    geo    = f.get("geometry", {}).get("coordinates", [None, None])
    addr   = p.get("address") or {}
    phones = p.get("phoneNumbers") or {}
    img    = p.get("image") or {}
    hours  = p.get("hours") or []
    hours_str = ""
    if hours:
        h = hours[0]
        if h.get("isClosed"):
            hours_str = "Closed today"
        elif h.get("isOpen24Hours"):
            hours_str = "Open 24 hours"
        else:
            def fmt_time(iso):
                try:
                    from datetime import datetime as _dt
                    t = _dt.fromisoformat(iso)
                    return t.strftime("%-I:%M %p").replace(":00 ", " ")
                except Exception:
                    return iso[11:16] if len(iso) > 15 else iso
            hours_str = f"{fmt_time(h.get('openTime',''))} – {fmt_time(h.get('closeTime',''))}"
    street  = addr.get("streetAddress", "")
    city    = addr.get("city", "")
    state   = addr.get("state", "")
    zipcode = addr.get("zip", "")
    return {
        "id":             str(p.get("storeNumber") or ""),
        "name":           p.get("name") or "",
        "short_name":     p.get("shortName") or "",
        "street":         street,
        "city":           city,
        "state":          state,
        "zip":            zipcode,
        "address":        f"{street}, {city}, {state} {zipcode}".strip(", "),
        "phone":          phones.get("Store", ""),
        "pharmacy_phone": phones.get("Pharmacy", ""),
        "hours_today":    hours_str,
        "hours_raw":      hours,
        "lat":            geo[0] if geo else None,
        "lng":            geo[1] if geo else None,
        "img_thumb":      img.get("thumbnail", ""),
        "img_hero":       img.get("hero", ""),
        "distance":       p.get("distance"),
    }


def _store_fetch(url: str, timeout: int = 8) -> dict:
    """Fetch from storelocator API, handling gzip transparently."""
    import gzip as _gzip
    req = urllib.request.Request(url, headers=_STORE_HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = _gzip.decompress(body)
        return json.loads(body)


def search_stores(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)  # PDC-16
    params = event.get("queryStringParameters") or {}
    query  = (params.get("q") or "").strip()
    if not query: return err("Missing ?q=")

    is_store_num = bool(re.match(r'^\d{3,4}$', query))
    is_zip       = bool(re.match(r'^\d{5}$', query))

    if is_store_num:
        url = (f"https://services.publix.com/storelocator/api/v1/stores/"
               f"?types=R,G,H,N,S&count=1&distance=1000&includeOpenAndCloseDates=true"
               f"&storeNumber={urllib.parse.quote(query)}&includeStore=true&isWebsite=true")
        try:
            raw      = _store_fetch(url)
            features = raw.get("features", [])
            stores   = [_parse_store_feature(f) for f in features if f.get("properties", {}).get("storeNumber")]
            return ok({"stores": stores[:1]})
        except Exception as e:
            print(f"[CW] search_stores lookup: {e}")
            return err("Store lookup failed.", 502)
    else:
        param = "zip" if is_zip else "city"
        url   = (f"https://services.publix.com/storelocator/api/v1/stores/"
                 f"?types=R,G,H,N,S&count=10&distance=50&includeOpenAndCloseDates=true"
                 f"&{param}={urllib.parse.quote(query)}&isWebsite=true")
        try:
            raw      = _store_fetch(url)
            features = raw.get("features", [])
            stores   = [_parse_store_feature(f) for f in features if f.get("properties", {}).get("storeNumber")]
            return ok({"stores": stores[:10]})
        except Exception as e:
            print(f"[CW] search_stores search: {e}")
            return err("Store search failed.", 502)


# ── Deals ─────────────────────────────────────────────────────────────────────

def _parse_deals_raw(raw: dict) -> list:
    """Parse raw Publix API response using the shared unified parser."""
    deals = []
    for d in (raw.get("Savings") or []):
        deals.append(parse_deal(d))
    return deals


def get_deals(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    types    = (params.get("types") or "").strip().lower()  # "core", "tpr", or "" (all)
    if not store_id: return err("Missing store_id parameter.")

    deals_table_obj = dynamodb.Table(os.environ["DEALS_TABLE"])
    cached          = deals_table_obj.get_item(Key={"store_id": store_id}).get("Item")

    if cached:
        num_chunks = int(cached.get("num_chunks", 0))
        updated_at = str(cached.get("fetched_at", ""))
        if num_chunks > 0:
            raw_deals = []
            for i in range(num_chunks):
                chunk_row = deals_table_obj.get_item(Key={"store_id": f"{store_id}#{i}"}).get("Item")
                if chunk_row:
                    raw_deals.extend(json.loads(chunk_row.get("deals", "[]")))
        else:
            raw_deals = json.loads(cached.get("deals", "[]"))
        total_all = len(raw_deals)
        # Pre-filter BEFORE parse_deal() to skip CPU work on excluded deals
        if types == "core":
            raw_deals = [d for d in raw_deals if (d.get("saving_type") or "").lower() != "tpr"]
        elif types == "tpr":
            raw_deals = [d for d in raw_deals if (d.get("saving_type") or "").lower() == "tpr"]
        # Re-enrich from cache via shared parser
        deals = [parse_deal(d) for d in raw_deals]
        _log_app_event("cache", "info", hit=True, store_id=store_id, endpoint="/deals", deal_count=len(deals))
    else:
        _hdrs = {
            "Accept":      "application/json, text/plain, */*",
            "Origin":      "https://www.publix.com",
            "Referer":     "https://www.publix.com/",
            "publixstore": str(store_id),
            "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        deals     = []
        seen_ids  = set()
        updated_at = ""
        fetch_failed = False
        for _url in [SAVINGS_URL_WEEKLY, SAVINGS_URL_COUPONS]:
            try:
                req = urllib.request.Request(_url, headers=_hdrs)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = json.loads(resp.read().decode())
                if not updated_at:
                    updated_at = raw.get("WeeklyAdLatestUpdatedDateTime", "")
                    _log_app_event("cache", "warn", hit=False, store_id=store_id, endpoint="/deals",
                                   message="cache miss — live fetch used")
                for d in _parse_deals_raw(raw):
                    if d["id"] not in seen_ids:
                        seen_ids.add(d["id"])
                        deals.append(d)
            except Exception as e:
                fetch_failed = True
                print(f"[CW] get_deals live fetch: {e}")
        if fetch_failed and not deals:
            return err("Deals temporarily unavailable.", 502)
        total_all = len(deals)
        # Post-filter for live fetch path (cached path pre-filters above)
        if types == "core":
            deals = [d for d in deals if d.get("saving_type", "").lower() != "tpr"]
        elif types == "tpr":
            deals = [d for d in deals if d.get("saving_type", "").lower() == "tpr"]

    counts = {
        "total":   len(deals),
        "weekly":  sum(1 for d in deals if d["saving_type"] == "WeeklyAd" and not d.get("is_bogo")),
        "extra":   sum(1 for d in deals if d["is_extra"]),
        "stacked": sum(1 for d in deals if d["is_stacked"]),
        "coupon":  sum(1 for d in deals if d["has_coupon"]),
        "publix":  sum(1 for d in deals if d["is_publix_brand"]),
    }
    dept_counts: dict = {}
    for d in deals:
        dept = (d.get("department") or "Other").strip()
        dept_counts[dept] = dept_counts.get(dept, 0) + 1
    saving_types = sorted({d["saving_type"] for d in deals if d.get("saving_type")})

    # Strip fields unused by the frontend to reduce payload (~40KB savings)
    # categories: only used for BOGO fallback — is_bogo already covers it
    # wa_promotion_type_id: never referenced in frontend JS
    _STRIP_FIELDS = ("categories", "wa_promotion_type_id")
    for d in deals:
        for f in _STRIP_FIELDS:
            d.pop(f, None)

    return ok({
        "deals":        deals,
        "total":        len(deals),
        "total_all":    total_all,
        "store_id":     store_id,
        "updated_at":   updated_at,
        "counts":       counts,
        "dept_counts":  dept_counts,
        "saving_types": saving_types,
    })


def get_deal_history(event):
    """GET /deals/history?store_id=X — weekly snapshots for price history."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    if not store_id: return err("Missing store_id parameter.")

    try:
        from boto3.dynamodb.conditions import Key as DKey
        resp = history_tbl.scan(
            FilterExpression="begins_with(store_id, :prefix)",
            ExpressionAttributeValues={":prefix": f"{store_id}#"},
        )
        rows = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = history_tbl.scan(
                FilterExpression="begins_with(store_id, :prefix)",
                ExpressionAttributeValues={":prefix": f"{store_id}#"},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            rows.extend(resp.get("Items", []))

        rows       = _to_py(rows)
        index_rows = []
        chunk_map  = {}

        for row in rows:
            key   = row.get("store_id", "")
            parts = key.split("#")
            if len(parts) >= 3:
                try:
                    chunk_idx = int(parts[-1])
                except ValueError:
                    index_rows.append(row)
                    continue
                base_key = "#".join(parts[:-1])
                chunk_map.setdefault(base_key, {})[chunk_idx] = json.loads(row.get("deals", "[]"))
            else:
                index_rows.append(row)

        snapshots = []
        for idx_row in index_rows:
            key        = idx_row.get("store_id", "")
            week       = idx_row.get("week", key.split("#")[-1] if "#" in key else "")
            num_chunks = int(idx_row.get("num_chunks", 0))
            chunks     = chunk_map.get(key, {})
            deals      = []
            for i in range(num_chunks):
                deals.extend(chunks.get(i, []))
            # Trim each deal to only fields the frontend needs for badges.
            # computeBadge() uses: title, savings
            # computeFreqHint() uses: title
            # Sending all 7+ fields per deal wastes ~60% of history payload.
            trimmed = [{"title": d.get("title", ""), "savings": d.get("savings", "")} for d in deals]
            snapshots.append({"week": week, "count": len(trimmed), "deals": trimmed})

        snapshots.sort(key=lambda s: s["week"], reverse=True)
        return ok({"store_id": store_id, "num_weeks": len(snapshots), "snapshots": snapshots})

    except Exception as e:
        print(f"[CW] get_deal_history: {e}")
        return err("Could not fetch deal history.", 500)


def get_deal_corpus(event):
    """GET /deals/corpus — global deduplicated deal title set for autocomplete."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    try:
        row    = corpus_tbl.get_item(Key={"corpus_id": "global"}).get("Item")
        raw    = row.get("titles") or set() if row else set()
        titles = sorted(raw)
        resp   = ok({"titles": titles, "count": len(titles)})
        resp["headers"] = {**resp.get("headers", {}), "Cache-Control": "max-age=86400"}
        return resp
    except Exception as e:
        print(f"[CW] get_deal_corpus: {e}")
        return err("Could not fetch corpus.", 500)


# ── Matches (on-the-fly) ─────────────────────────────────────────────────────

def get_matches(event):
    """GET /user/matches — compute matches on-the-fly from cached deals + user items.

    Returns the same shape as the scraper's match output, ensuring web UI
    and email alerts produce identical results from the shared matcher.
    """
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)

    # Load user prefs
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)
    prefs     = _to_py(user.get("prefs", DEFAULT_PREFS))
    items     = prefs.get("items") or []
    matching  = prefs.get("matching") or {}
    store_id  = (prefs.get("store_id") or "").strip()

    if not store_id:
        return ok({"matches": [], "total": 0, "message": "No store selected."})
    if not items:
        return ok({"matches": [], "total": 0, "message": "No items on list."})

    # Load deals from cache
    deals_table_obj = dynamodb.Table(os.environ["DEALS_TABLE"])
    cached = deals_table_obj.get_item(Key={"store_id": store_id}).get("Item")

    if not cached:
        return ok({"matches": [], "total": 0, "message": "No deals cached for this store."})

    num_chunks = int(cached.get("num_chunks", 0))
    if num_chunks > 0:
        raw_deals = []
        for i in range(num_chunks):
            chunk_row = deals_table_obj.get_item(Key={"store_id": f"{store_id}#{i}"}).get("Item")
            if chunk_row:
                raw_deals.extend(json.loads(chunk_row.get("deals", "[]")))
    else:
        raw_deals = json.loads(cached.get("deals", "[]"))

    # Re-enrich via shared parser
    deals = [parse_deal(d) for d in raw_deals]

    # Run matcher
    matches = match_deals_for_user(deals, items, matching)

    # Enrich match results with full deal fields the frontend needs for rendering
    # (saving_type, is_bogo, has_coupon, coupon_id, etc.)
    deals_by_id = {d["id"]: d for d in deals if d.get("id")}
    # Also index by normalized title for fallback lookup
    deals_by_title = {d.get("title", "").strip().lower(): d for d in deals if d.get("title")}
    for m in matches:
        # Prefer id emitted directly by matcher; fall back to title lookup
        deal_id = m.get("id") or None
        if not deal_id or deal_id not in deals_by_id:
            deal_id = None
            norm_name = m.get("deal_name", "").strip().lower()
            d_by_title = deals_by_title.get(norm_name)
            if d_by_title:
                deal_id = d_by_title.get("id")
        if deal_id and deal_id in deals_by_id:
            src = deals_by_id[deal_id]
            m["id"]                   = src.get("id", "")
            m["saving_type"]          = src.get("saving_type", "")
            m["is_bogo"]              = src.get("is_bogo", False)
            m["has_coupon"]           = src.get("has_coupon", False)
            m["coupon_id"]            = src.get("coupon_id", "")
            m["is_extra"]             = src.get("is_extra", False)
            m["is_publix_brand"]      = src.get("is_publix_brand", False)
            m["categories"]           = src.get("categories", [])
            m["savings_per_item"]     = src.get("savings_per_item")
            m["final_price"]          = src.get("final_price", 0)
            m["wa_promotion_type_id"] = src.get("wa_promotion_type_id", 1)
            # Map deal_name to title for frontend consistency
            m["title"] = m["deal_name"]

    return ok({
        "matches":     matches,
        "total":       len(matches),
        "store_id":    store_id,
        "sensitivity": matching.get("sensitivity", "normal"),
    })
