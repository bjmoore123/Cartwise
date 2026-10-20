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
from logging_utils import _log_app_event, count_cache_event
from shared.deal_parser import parse_deal
from shared.matcher import match_deals_for_user, _get_threshold
from history import (build_index, compute_badges, item_frequency, title_points, autocomplete_terms,
                     serialize_index, deserialize_index, SUMMARY_FMT)

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
            return ok({"stores": stores[:1]}, _headers={"Cache-Control": "max-age=300"})
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
            return ok({"stores": stores[:10]}, _headers={"Cache-Control": "max-age=300"})
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


def _chunk_key(store_id: str, gen: str, i: int) -> str:
    """Chunk row key. v9.2.2 (#6): chunks carry the cache generation so a new
    scrape never overwrites the rows a reader is using; legacy rows have no gen."""
    return f"{store_id}#{gen}#{i}" if gen else f"{store_id}#{i}"


def _read_cached_deals(table, store_id: str, idx_row: dict, _retry: bool = True) -> list:
    """Raw deal dicts for a cached store, from the index row's chunks (or inline `deals`).

    If a chunk is missing (a scrape swapped generations between our two reads),
    re-read the index once and try again with the new generation.
    """
    num_chunks = int(idx_row.get("num_chunks", 0))
    if num_chunks <= 0:
        return json.loads(idx_row.get("deals", "[]"))
    gen = str(idx_row.get("gen", "") or "")
    raw_deals = []
    for i in range(num_chunks):
        chunk_row = table.get_item(Key={"store_id": _chunk_key(store_id, gen, i)}).get("Item")
        if not chunk_row:
            if _retry:
                fresh = table.get_item(Key={"store_id": store_id}).get("Item") or {}
                if str(fresh.get("gen", "") or "") != gen:
                    return _read_cached_deals(table, store_id, fresh, _retry=False)
            print(f"[CW] missing deal chunk {i} for store {store_id} (gen={gen or 'legacy'})")
            continue
        raw_deals.extend(json.loads(chunk_row.get("deals", "[]")))
    return raw_deals


def get_deals(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    types    = (params.get("types") or "").strip().lower()  # "core", "tpr", or "" (all)
    if not store_id: return err("Missing store_id parameter.")

    deals_table_name = os.environ["DEALS_TABLE"]
    deals_table_obj  = dynamodb.Table(deals_table_name)
    cached           = deals_table_obj.get_item(Key={"store_id": store_id}).get("Item")

    if cached:
        updated_at = str(cached.get("fetched_at", ""))
        raw_deals  = _read_cached_deals(deals_table_obj, store_id, cached)
        total_all = len(raw_deals)
        # Pre-filter BEFORE parse_deal() to skip CPU work on excluded deals
        if types == "core":
            raw_deals = [d for d in raw_deals if (d.get("saving_type") or "").lower() != "tpr"]
        elif types == "tpr":
            raw_deals = [d for d in raw_deals if (d.get("saving_type") or "").lower() == "tpr"]
        # Re-enrich from cache via shared parser
        deals = [parse_deal(d) for d in raw_deals]
        if types != "tpr":          # count once per page load (core + tpr are one load)
            count_cache_event(store_id, hit=True)
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
        if types != "tpr":
            count_cache_event(store_id, hit=False)
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
        for f in ("regular_price", "additional_savings"):   # v9.3: only send when set
            if not d.get(f):
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
    }, _headers={"Cache-Control": "private, max-age=300"})   # v9.5: authenticated data; never in shared caches


# History index cache (v9.2.1).
#   1. Warm Lambda memory:  _HISTORY_CACHE[store_id] = (version, index)
#   2. DynamoDB summary:    sk "_summary" (meta) + "_summary#N" (gzip chunks), written on first build
#   3. Full rebuild:        read every weekly snapshot (~4.7 MB at 30 weeks), then persist (2)
# The scraper deletes "_summary" whenever it writes a new weekly snapshot, so the next
# request rebuilds. As a safety net, a summary older than SUMMARY_MAX_AGE_DAYS is rebuilt.
_HISTORY_CACHE: dict = {}
SUMMARY_SK            = "_summary"
SUMMARY_CHUNK_BYTES   = 350_000      # DynamoDB item limit is 400 KB
SUMMARY_MAX_AGE_DAYS  = 8


def _load_history_snapshots(store_id: str) -> list:
    """Read every weekly snapshot (index rows + chunk rows) for a store."""
    from boto3.dynamodb.conditions import Key as DKey
    rows, kwargs = [], {"KeyConditionExpression": DKey("store_id").eq(store_id)}
    while True:
        resp = history_tbl.query(**kwargs)
        rows.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    index_rows, chunk_map = [], {}
    for row in rows:
        sk = str(row.get("sk", ""))
        if sk.startswith("_"):              # summary rows, not snapshots
            continue
        if "#" in sk:
            week_key, _, idx = sk.rpartition("#")
            try:
                chunk_map.setdefault(week_key, {})[int(idx)] = json.loads(row.get("deals", "[]"))
                continue
            except ValueError:
                pass
        index_rows.append(_to_py(row))

    snapshots = []
    for idx_row in index_rows:
        week   = idx_row.get("week", idx_row.get("sk", ""))
        chunks = chunk_map.get(week, {})
        deals  = []
        for i in range(int(idx_row.get("num_chunks", 0))):
            deals.extend(chunks.get(i, []))
        snapshots.append({"week": week, "deals": deals})
    return snapshots


def _summary_is_fresh(meta: dict) -> bool:
    from datetime import datetime, timezone, timedelta
    if int(meta.get("fmt", 2)) != SUMMARY_FMT:       # v9.4: older format has no prices
        return False
    try:
        built = datetime.fromisoformat(str(meta.get("built_at", "")))
        return datetime.now(timezone.utc) - built < timedelta(days=SUMMARY_MAX_AGE_DAYS)
    except ValueError:
        return False


def _read_persisted_index(store_id: str, meta: dict):
    blob = b""
    for i in range(int(meta.get("num_chunks", 0))):
        row = history_tbl.get_item(Key={"store_id": store_id, "sk": f"{SUMMARY_SK}#{i}"}).get("Item")
        if not row:
            return None                     # partially written or expired: rebuild
        data = row.get("blob")
        blob += data.value if hasattr(data, "value") else bytes(data)
    return deserialize_index(blob)


def _persist_index(store_id: str, index: dict) -> str:
    from datetime import datetime, timezone, timedelta
    now     = datetime.now(timezone.utc)
    version = now.isoformat()
    blob    = serialize_index(index)
    parts   = [blob[i:i + SUMMARY_CHUNK_BYTES] for i in range(0, len(blob), SUMMARY_CHUNK_BYTES)] or [b""]
    expires = int((now + timedelta(days=SUMMARY_MAX_AGE_DAYS + 2)).timestamp())
    for i, part in enumerate(parts):        # chunks first, meta last: readers never see a half-written set
        history_tbl.put_item(Item={"store_id": store_id, "sk": f"{SUMMARY_SK}#{i}",
                                   "blob": part, "version": version, "expires_at": expires})
    history_tbl.put_item(Item={"store_id": store_id, "sk": SUMMARY_SK, "version": version,
                               "num_chunks": len(parts), "built_at": version,
                               "newest_week": index["weeks"][0] if index["weeks"] else "",
                               "fmt": SUMMARY_FMT,
                               "bytes": len(blob), "expires_at": expires})
    return version


def _history_index(store_id: str) -> dict:
    meta = history_tbl.get_item(Key={"store_id": store_id, "sk": SUMMARY_SK}).get("Item")
    if meta and _summary_is_fresh(meta):
        version = str(meta.get("version", ""))
        cached = _HISTORY_CACHE.get(store_id)
        if cached and cached[0] == version:
            return cached[1]
        index = _read_persisted_index(store_id, meta)
        if index is not None:
            _HISTORY_CACHE[store_id] = (version, index)
            return index

    index = build_index(_load_history_snapshots(store_id))
    try:
        version = _persist_index(store_id, index)
    except Exception as e:                  # persisting is an optimisation; never fail the request
        print(f"[CW] history summary persist failed: {e}")
        version = "unpersisted"
    _HISTORY_CACHE[store_id] = (version, index)
    return index


def get_deal_history(event):
    """GET /deals/history?store_id=X            → badge + frequency summary (a few KB)
       GET /deals/history?store_id=X&title=T    → chart points for one deal title

    v9.2: the old response shipped every past deal (4.7 MB / 30 weeks) and the
    browser fuzzy-matched it against every current deal (~190 s CPU). The
    summary is now computed here, from a per-store index cached in the warm Lambda.
    """
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    title    = (params.get("title") or "").strip()
    if not store_id: return err("Missing store_id parameter.")

    try:
        index     = _history_index(store_id)
        num_weeks = len(index["weeks"])
        base = {
            "version":          2,
            "store_id":         store_id,
            "num_weeks":        num_weeks,
            "weeks_of_history": max(num_weeks - 1, 1),
        }
        if title:
            # v9.8.2: store weeks oldest first, so the chart can mark weeks it was NOT on sale
            return ok({**base, "title": title, "points": title_points(index, title),
                       "weeks": sorted(index["weeks"]), "newest_week": max(index["weeks"], default="")},
                      _headers={"Cache-Control": "private, max-age=300"})

        items = []
        try:
            user  = users_table.get_item(Key={"email": sess["email"]}).get("Item") or {}
            items = (_to_py(user.get("prefs") or {}).get("items")) or []
        except Exception as e:
            print(f"[CW] get_deal_history prefs: {e}")

        return ok({
            **base,
            "newest_week": index["weeks"][0] if index["weeks"] else "",
            "badges":      compute_badges(index),
            "freq":        item_frequency(index, items),
        }, _headers={"Cache-Control": "private, max-age=60"})

    except Exception as e:
        print(f"[CW] get_deal_history: {e}")
        return err("Could not fetch deal history.", 500)


def _gzip_ok(event, body: dict, headers: dict) -> dict:
    """200 JSON response, gzipped when the browser accepts it (v9.8.4).
    The API's CloudFront behavior has caching (and so compression) disabled."""
    import base64, gzip as _gz
    resp = ok(body, _event=event, _headers=headers)
    accept = {k.lower(): v for k, v in (event.get("headers") or {}).items()}.get("accept-encoding", "")
    if "gzip" in accept.lower():
        resp["body"] = base64.b64encode(_gz.compress(resp["body"].encode("utf-8"), 6)).decode("ascii")
        resp["isBase64Encoded"] = True
        resp["headers"] = {**resp["headers"], "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
    return resp


def get_deal_corpus(event):
    """GET /deals/corpus?store_id=X — autocomplete terms.

    v9.8.4: every deal title and brand this store ran in the last 52 weeks, from
    the history index (already cached per store). Bounded by the history window,
    so it no longer grows forever. The old global corpus row stopped updating on
    2026-04-08 (DynamoDB 400 KB item limit); it is now only a read-only fallback
    for a store with no history yet.
    """
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    store_id = ((event.get("queryStringParameters") or {}).get("store_id") or "").strip()
    try:
        titles, source = [], "global"
        if store_id:
            try:
                index = _history_index(store_id)
                if index["weeks"]:
                    titles, source = autocomplete_terms(index), "history"
            except Exception as e:
                print(f"[CW] corpus from history ({store_id}): {e}")
        if source == "global":
            row = corpus_tbl.get_item(Key={"corpus_id": "global"}).get("Item") or {}
            # v9.2.2 (#5): `terms` is the scraper's attribute, `titles` the legacy name
            titles = sorted(set(row.get("terms") or set()) | set(row.get("titles") or set()))
        return _gzip_ok(event, {"titles": titles, "count": len(titles), "source": source,
                                "store_id": store_id},
                        {"Cache-Control": "private, max-age=21600"})
    except Exception as e:
        print(f"[CW] get_deal_corpus: {e}")
        return err("Could not fetch corpus.", 500)


# ── Matches (on-the-fly) ─────────────────────────────────────────────────────

def get_matches(event):
    """GET /user/matches — compute matches on-the-fly from cached deals + user items.

    v9.7 decision: kept. The web page matches in the browser (same algorithm,
    parity-tested), so the page no longer calls this; it stays as the server-side
    "what would this week's email contain" check, used by support and tests.

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

    raw_deals = _read_cached_deals(deals_table_obj, store_id, cached)

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
            m["is_stacked"]           = src.get("is_stacked", False)
            m["offer"]                = src.get("offer") or {}        # v9.3
            if src.get("regular_price"):      m["regular_price"]      = src["regular_price"]
            if src.get("additional_savings"): m["additional_savings"] = src["additional_savings"]
            # Map deal_name to title for frontend consistency
            m["title"] = m["deal_name"]

    return ok({
        "matches":     matches,
        "total":       len(matches),
        "store_id":    store_id,
        # v9.7: report what the matcher actually used (was "normal" even with a slider value)
        "threshold":   _get_threshold(matching),
    })
