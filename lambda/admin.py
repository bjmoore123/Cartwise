"""lambda/admin.py — Admin-only endpoints.

Architecture #1: @require_admin decorator replaces per-function auth checks.
PDC-08: admin_reset_email has duplicate-check guard.
"""
import re
import json
import secrets
from collections import Counter
from datetime import datetime, timezone, timedelta
from functools import wraps

from helpers import (
    ok, err, _to_py, get_admin_auth, hash_pin,
    users_table, sessions_table, admin_sessions_tbl,
    scrape_logs_tbl, auth_logs_tbl, app_logs_tbl,
    lambda_client, logs_client,
    DEFAULT_PREFS, SCRAPER_FUNCTION, LOG_GROUP,
    ADMIN_SECRET, ADMIN_SESSION_TTL_HOURS,
    EMAIL_RE, PIN_DENYLIST, ALLOWED_PREF_KEYS,
    cors_headers,
)
from auth import register


import secrets as _secrets
from datetime import timedelta


# ── Admin session auth (PDC-04) ───────────────────────────────────────────────

def admin_login(event, body):
    """POST /admin/login — exchange raw secret for a short-lived session token.
    PDC-04: admin session tokens expire after ADMIN_SESSION_TTL_HOURS (12h).
    PDC-21: token set as HttpOnly cookie (was returned in JSON / stored in sessionStorage).
    Uses hmac.compare_digest to prevent timing attacks (PDC-15).
    """
    import hmac as _hmac
    if not ADMIN_SECRET:
        return err("Admin access not configured.", 503)
    candidate = (body.get("secret") or "").strip()
    if not candidate or not _hmac.compare_digest(candidate, ADMIN_SECRET):
        return err("Incorrect admin secret.", 401)

    now        = datetime.now(timezone.utc)
    token      = _secrets.token_hex(32)
    expires_at = int((now + timedelta(hours=ADMIN_SESSION_TTL_HOURS)).timestamp())
    admin_sessions_tbl.put_item(Item={
        "token":      token,
        "created_at": now.isoformat(),
        "expires_at": expires_at,
    })
    # PDC-21: set HttpOnly cookie — XSS can no longer steal admin token
    cookie = (
        f"admin_session={token}; HttpOnly; Secure; SameSite=Lax; "
        f"Domain=cartwise.shopping; Path=/; Max-Age={ADMIN_SESSION_TTL_HOURS * 3600}"
    )
    response = ok({"message": "Admin session started.", "expires_in": ADMIN_SESSION_TTL_HOURS * 3600},
                  _event=event)
    response["cookies"] = [cookie]
    return response


def admin_logout_session(event):
    """POST /admin/logout — delete the current admin session token.
    PDC-21: reads token from HttpOnly cookie."""
    # Read token from cookie (primary) or header (fallback during transition)
    token = _get_admin_token_from_event(event)
    if token:
        try:
            admin_sessions_tbl.delete_item(Key={"token": token})
        except Exception as e:
            print(f"[CW] admin_logout_session: {e}")
    # Clear cookie
    response = ok({"message": "Admin session ended."}, _event=event)
    response["cookies"] = ["admin_session=; HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping; Path=/; Max-Age=0"]
    return response


def _get_admin_token_from_event(event) -> str:
    """Extract admin session token from HttpOnly cookie or Authorization header."""
    # PDC-21: prefer cookie
    cookies = event.get("cookies") or []
    for part in cookies:
        part = part.strip()
        if part.startswith("admin_session="):
            return part[len("admin_session="):].strip()
    # Fallback: Cookie header (payload format 1.0)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    cookie_header = headers.get("cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                return part[len("admin_session="):].strip()
    # Legacy fallback: Authorization header (transition period)
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("admintoken "):
        return auth[len("admintoken "):].strip()
    return ""


# ── Decorator ─────────────────────────────────────────────────────────────────

def require_admin(f):
    """Decorator that gates a function behind admin auth (PDC-10/15 compliant)."""
    @wraps(f)
    def wrapper(event, *args, **kwargs):
        if not get_admin_auth(event):
            return err("Unauthorized.", 401)
        return f(event, *args, **kwargs)
    return wrapper


# ── User management ───────────────────────────────────────────────────────────

@require_admin
def admin_list_users(event):
    """GET /admin/users?cursor=...&limit=50 — paginated user list."""
    try:
        qs = event.get("queryStringParameters") or {}
        limit = min(int(qs.get("limit", 50)), 200)
        cursor = qs.get("cursor", "")

        scan_kwargs = {"Limit": limit + 1}  # fetch one extra to detect next page
        if cursor:
            scan_kwargs["ExclusiveStartKey"] = {"email": cursor}

        resp = users_table.scan(**scan_kwargs)
        items = _to_py(resp.get("Items", []))

        users = []
        for u in items:
            if u.get("email") == "__meta__": continue
            p = u.get("prefs") or {}
            users.append({
                "email":          u["email"],
                "created_at":     u.get("created_at", ""),
                "store_id":       p.get("store_id", ""),
                "store_name":     p.get("store_name", ""),
                "notify_email":   p.get("notify_email") or u["email"],
                "item_count":     len(p.get("items") or []),
                "items":          p.get("items") or [],
                "sensitivity":    (p.get("matching") or {}).get("sensitivity", "normal"),
                "email_verified": u.get("email_verified", True),
            })

        # Determine next cursor
        next_cursor = None
        if len(users) > limit:
            users = users[:limit]
            next_cursor = users[-1]["email"]
        elif "LastEvaluatedKey" in resp:
            next_cursor = resp["LastEvaluatedKey"].get("email", "")

        result = {"users": users, "total": len(users)}
        if next_cursor:
            result["next_cursor"] = next_cursor
        return ok(result)
    except Exception as e:
        print(f"[CW] admin_list_users: {e}")
        return err("Failed to list users.", 500)


@require_admin
def admin_get_user(event, email: str):
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    safe = _to_py(user)
    safe.pop("pin_hash", None)
    return ok({"user": safe})


@require_admin
def admin_create_user(event, body):
    return register(body, event=event, _admin=True)


@require_admin
def admin_delete_user(event, email: str):
    if not email: return err("Missing email.")
    users_table.delete_item(Key={"email": email})
    try:
        users_table.update_item(
            Key={"email": "__meta__"},
            UpdateExpression="ADD deleted_count :one SET last_deleted_at = :ts",
            ExpressionAttributeValues={":one": 1, ":ts": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        print(f"[CW] admin_delete_user meta update: {e}")
    return ok({"message": f"User {email} deleted."})


@require_admin
def admin_reset_pin(event, body, email: str):
    new_pin = str(body.get("new_pin") or "").strip()
    if not re.match(r'^\d{4}$', new_pin): return err("New PIN must be 4 digits.")
    if new_pin in PIN_DENYLIST: return err("That PIN is too common.")
    if not users_table.get_item(Key={"email": email}).get("Item"):
        return err("User not found.", 404)
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET pin_hash = :p",
        ExpressionAttributeValues={":p": hash_pin(new_pin)},
    )
    return ok({"message": f"PIN reset for {email}."})


@require_admin
def admin_reset_email(event, body, email: str):
    """PDC-08: duplicate-email check prevents silent overwrite; updates notify_email in prefs."""
    new_email = (body.get("new_email") or "").strip().lower()
    if not new_email.isascii() or not re.match(EMAIL_RE, new_email):
        return err("Invalid email address.")
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    if users_table.get_item(Key={"email": new_email}).get("Item"):
        return err("An account with that email already exists.", 409)
    new_item = {**user, "email": new_email}
    prefs    = _to_py(new_item.get("prefs") or {})
    if prefs.get("notify_email", "").lower() == email.lower():
        prefs["notify_email"] = new_email
        new_item["prefs"]     = prefs
    users_table.put_item(Item=new_item)
    users_table.delete_item(Key={"email": email})
    return ok({"message": f"Email changed from {email} to {new_email}."})


@require_admin
def admin_patch_prefs(event, body, email: str):
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    existing = _to_py(user.get("prefs", DEFAULT_PREFS))
    # PDC-21: filter to allowed keys — same as user-facing save_prefs
    incoming = body.get("prefs", body)
    filtered = {k: v for k, v in incoming.items() if k in ALLOWED_PREF_KEYS}
    merged   = {**existing, **filtered}
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": merged},
    )
    return ok({"message": f"Prefs updated for {email}.", "prefs": merged})


@require_admin
def admin_clear_items(event, email: str):
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    prefs["items"] = []
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs},
    )
    return ok({"message": f"Items cleared for {email}."})


@require_admin
def admin_resend_verification(event, email: str):
    """POST /admin/users/{email}/resend-verification — resend verification email (no rate limit)."""
    from auth import _send_verification_email, VERIFY_TOKEN_TTL_HOURS
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    user = _to_py(user)
    if user.get("email_verified"):
        return ok({"message": f"{email} is already verified."})
    verify_token = secrets.token_urlsafe(32)
    verify_token_exp = (
        datetime.now(timezone.utc) + timedelta(hours=VERIFY_TOKEN_TTL_HOURS)
    ).isoformat()
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET verify_token = :t, verify_token_exp = :e",
        ExpressionAttributeValues={":t": verify_token, ":e": verify_token_exp},
    )
    _send_verification_email(email, verify_token)
    return ok({"message": f"Verification email resent to {email}."})


@require_admin
def admin_stats(event):
    items, resp = [], users_table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = users_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    users = [u for u in _to_py(items) if u.get("email") != "__meta__"]
    total = len(users)

    by_month   = Counter()
    by_store   = Counter()
    all_items  = Counter()
    item_counts = []

    for u in users:
        ts = u.get("created_at", "")
        if ts: by_month[ts[:7]] += 1
        p    = u.get("prefs") or {}
        name = p.get("store_name") or ""
        sid  = p.get("store_id") or ""
        if name:
            parts = [x.strip() for x in name.split(",")]
            city  = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else sid or "Unknown")
            by_store[city] += 1
        elif sid:
            by_store[f"Store #{sid}"] += 1
        else:
            by_store["No store set"] += 1
        item_list = p.get("items") or []
        item_counts.append(len(item_list))
        for item in item_list:
            if item and isinstance(item, str):
                all_items[item.strip().lower()] += 1

    avg_items = round(sum(item_counts) / total, 1) if total else 0
    no_items  = sum(1 for c in item_counts if c == 0)
    meta      = _to_py(users_table.get_item(Key={"email": "__meta__"}).get("Item") or {})

    return ok({
        "total_users":   total,
        "deleted_users": int(meta.get("deleted_count", 0)),
        "avg_items":     avg_items,
        "no_items_count": no_items,
        "by_month":      [{"month": k, "count": v} for k, v in sorted(by_month.items())],
        "by_geography":  [{"location": k, "count": v} for k, v in by_store.most_common(20)],
        "popular_items": [{"item": k, "count": v} for k, v in all_items.most_common(25)],
    })


# ── Scrape logs & triggers ────────────────────────────────────────────────────

@require_admin
def get_scrape_logs(event):
    try:
        resp  = scrape_logs_tbl.scan(Limit=50)
        items = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return ok({"logs": items[:15]})
    except Exception as e:
        print(f"[CW] admin_scrape_logs: {e}")
        return err("Could not fetch scrape logs.", 500)


@require_admin
def invoke_scraper(event):
    try:
        lambda_client.invoke(FunctionName=SCRAPER_FUNCTION, InvocationType="Event")
        return ok({"message": "Scraper invoked. Check logs in ~30 seconds."})
    except Exception as e:
        print(f"[CW] admin_scrape_now: {e}")
        return err("Failed to invoke scraper.", 500)


@require_admin
def get_log_tail(event):
    try:
        streams = logs_client.describe_log_streams(
            logGroupName=LOG_GROUP, orderBy="LastEventTime",
            descending=True, limit=3,
        ).get("logStreams", [])
        if not streams: return ok({"lines": [], "stream": None})
        all_lines = []
        for stream_info in streams[:2]:
            stream = stream_info["logStreamName"]
            events = logs_client.get_log_events(
                logGroupName=LOG_GROUP, logStreamName=stream,
                limit=150, startFromHead=False,
            ).get("events", [])
            for e in events:
                ts = datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc)
                all_lines.append({"ts": ts.isoformat(), "msg": e["message"].rstrip("\n")})
        all_lines.sort(key=lambda x: x["ts"])
        return ok({"lines": all_lines[-150:], "stream": streams[0]["logStreamName"]})
    except Exception as e:
        print(f"[CW] admin_logs_tail: {e}")
        return err("Could not fetch logs.", 500)


# ── Auth logs & app logs ──────────────────────────────────────────────────────

@require_admin
def get_auth_logs(event):
    try:
        params = event.get("queryStringParameters") or {}
        limit  = min(int(params.get("limit", 100)), 500)
        resp   = auth_logs_tbl.scan(Limit=limit)
        items  = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit]})
    except Exception as e:
        print(f"[CW] admin_auth_logs: {e}")
        return err("Could not fetch auth logs.", 500)


@require_admin
def get_app_logs(event):
    try:
        params = event.get("queryStringParameters") or {}
        limit  = min(int(params.get("limit", 200)), 500)
        source = (params.get("source") or "").strip()

        # DynamoDB Scan `Limit` caps items *examined*, not returned — a small
        # Limit will miss sparse sources (e.g. email) buried beyond the first page.
        # Solution: paginate the full table, applying a server-side FilterExpression
        # when a specific source is requested to reduce read volume.
        scan_kwargs = {}
        if source:
            from boto3.dynamodb.conditions import Attr
            scan_kwargs["FilterExpression"] = Attr("source").eq(source)

        items, resp = [], app_logs_tbl.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = app_logs_tbl.scan(
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                **scan_kwargs,
            )
            items.extend(resp.get("Items", []))

        items = _to_py(items)
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit]})
    except Exception as e:
        print(f"[CW] admin_app_logs: {e}")
        return err("Could not fetch app logs.", 500)


@require_admin
def admin_inbound_logs(event):
    """GET /admin/inbound-logs — app-log entries for inbound email import events."""
    try:
        from boto3.dynamodb.conditions import Attr
        params = event.get("queryStringParameters") or {}
        limit  = min(int(params.get("limit", 200)), 500)
        sender = (params.get("sender") or "").strip().lower()
        fe     = Attr("action").begins_with("inbound-")
        if sender:
            fe = fe & (Attr("sender").eq(sender) | Attr("user").eq(sender))
        resp  = app_logs_tbl.scan(FilterExpression=fe, Limit=2000)
        items = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit], "sender_filter": sender or None})
    except Exception as e:
        print(f"[CW] admin_inbound_logs: {e}")
        return err("Could not fetch inbound logs.", 500)
