"""
lambda/api.py  (v4)

API Gateway Lambda handler for Cartwise.
Auth: email + 4-digit PIN. Sessions stored in DynamoDB.
Admin: separate ADMIN_SECRET token.

User Routes:
  POST /auth/register
  POST /auth/login
  POST /auth/logout
  POST /auth/change-pin
  GET  /user/prefs
  PUT  /user/prefs
  DELETE /user/account
  GET  /stores/search?q=...
  GET  /deals?store_id=...

Admin Routes (Authorization: AdminSecret <secret>):
  GET    /admin/users
  POST   /admin/users                   { email, pin }
  GET    /admin/users/{email}
  DELETE /admin/users/{email}
  POST   /admin/users/{email}/reset-pin { new_pin }
  PUT    /admin/users/{email}/prefs     { prefs }
  DELETE /admin/users/{email}/items
  GET    /admin/scrape-logs
  POST   /admin/scrape-now
  GET    /admin/logs/tail
"""

import json
import os
import hashlib
import hmac
import secrets
import re
import urllib.request
import urllib.parse
import boto3
from decimal import Decimal
from datetime import datetime, timezone, timedelta

_region         = os.environ.get("PDC_REGION", "us-east-1")
dynamodb        = boto3.resource("dynamodb")
lambda_client   = boto3.client("lambda", region_name=_region)
logs_client     = boto3.client("logs",   region_name=_region)

users_table     = dynamodb.Table(os.environ["USERS_TABLE"])
sessions_table  = dynamodb.Table(os.environ["SESSIONS_TABLE"])
scrape_logs_tbl = dynamodb.Table(os.environ.get("SCRAPE_LOGS_TABLE", "cartwise-scrape-logs"))
auth_logs_tbl   = dynamodb.Table(os.environ.get("AUTH_LOGS_TABLE",  "cartwise-auth-logs"))
app_logs_tbl    = dynamodb.Table(os.environ.get("APP_LOGS_TABLE",   "cartwise-app-logs"))
history_tbl     = dynamodb.Table(os.environ.get("HISTORY_TABLE",   "cartwise-deal-history"))
corpus_tbl      = dynamodb.Table(os.environ.get("CORPUS_TABLE",    "cartwise-deal-corpus"))

# Strip any accidental whitespace/newlines that can sneak in via Lambda env vars
ADMIN_SECRET     = (os.environ.get("ADMIN_SECRET", "") or "").strip()
SCRAPER_FUNCTION = os.environ.get("SCRAPER_FUNCTION", "cartwise-scraper")
LOG_GROUP        = f"/aws/lambda/{SCRAPER_FUNCTION}"
# Secret salt for HMAC-based unsubscribe tokens. Rotate to invalidate all existing links.
UNSUB_SECRET     = (os.environ.get("UNSUB_SECRET", "changeme-set-in-deploy") or "").strip()
FRONTEND_URL     = (os.environ.get("FRONTEND_URL", "") or "").strip()
API_URL          = (os.environ.get("API_URL", "") or "").strip()
INBOUND_EMAIL_ADDR    = (os.environ.get("INBOUND_EMAIL_ADDR", "") or "").strip()
RESEND_WEBHOOK_SECRET = (os.environ.get("RESEND_WEBHOOK_SECRET", "") or "").strip()

CORS = {
    "Access-Control-Allow-Origin":  FRONTEND_URL if FRONTEND_URL else "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Content-Type": "application/json",
}

SESSION_TTL_HOURS = 72

_SAVINGS_BASE = (
    "https://services.publix.com/api/v4/savings"
    "?smImg=235&enImg=368&fallbackImg=false&isMobile=false"
    "&page=1&pageSize=0&includePersonalizedDeals=false"
    "&languageID=1&isWeb=true"
)
# KEY: publixstore must be sent as a REQUEST HEADER (not query param)
# WeeklyAd and AllDeals are separate pools that must both be fetched
SAVINGS_URL_WEEKLY  = _SAVINGS_BASE + "&getSavingType=WeeklyAd"
SAVINGS_URL_COUPONS = _SAVINGS_BASE + "&getSavingType=AllDeals"
SAVINGS_URL = SAVINGS_URL_COUPONS  # legacy alias

DEFAULT_PREFS = {
    "store_id":      "",
    "store_name":    "",
    "store_address": "",
    "notify_email":  "",
    "email_enabled": True,
    "items":         [],
    "matching":      {"threshold": 75},
    "notifications": {"only_matches": True},
}


# ── helpers ───────────────────────────────────────────────────────────────────

class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)

def ok(body, status=200):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body, cls=_Enc)}

def err(msg, status=400, _event=None, _path=None, _method=None, _extra=None):
    if status >= 400 and status not in (401, 404, 429):
        # Log 4xx/5xx errors (skip auth failures, lockouts, and 404s to reduce noise)
        try:
            _log_app_event("api", "error" if status >= 500 else "warn",
                           status=status, message=str(msg)[:300],
                           path=_path or "", method=_method or "")
        except Exception:
            pass
    body = {"error": msg}
    if _extra:
        body.update(_extra)
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}

def hash_pin(pin: str, salt: bytes = None) -> str:
    """Hash a PIN with scrypt + random salt.
    Returns 'scrypt$<hex_salt>$<hex_hash>'.
    salt is generated fresh when not supplied (register / change-pin).
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_pin(pin: str, stored_hash: str) -> bool:
    """Verify a PIN against a stored hash.
    Handles both scrypt format and legacy unsalted SHA-256.
    Legacy hashes always fail — user must reset their PIN.
    """
    if not stored_hash or "$" not in stored_hash:
        # Legacy unsalted SHA-256 — reject, force PIN reset via re-registration
        return False
    try:
        parts = stored_hash.split("$")
        if parts[0] != "scrypt" or len(parts) != 3:
            return False
        salt = bytes.fromhex(parts[1])
        h    = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), parts[2])
    except Exception:
        return False

# ── Unsubscribe token helpers ─────────────────────────────────────────────────
# Token = HMAC-SHA256(secret, email) — deterministic per user, no DB needed.
# Scope is strictly limited: the token only permits toggling email_enabled for
# the exact email it was generated for. Rotating UNSUB_SECRET invalidates all
# existing links (useful if links are ever compromised).

def _unsub_token(email: str) -> str:
    return hmac.new(
        UNSUB_SECRET.encode(),
        email.lower().strip().encode(),
        hashlib.sha256,
    ).hexdigest()

def _verify_unsub_token(email: str, token: str) -> bool:
    expected = _unsub_token(email)
    # Use compare_digest to avoid timing attacks
    return hmac.compare_digest(expected, token.lower().strip())

def _to_py(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):  return {k: _to_py(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_to_py(i) for i in obj]
    return obj

def get_session(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token   = headers.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    if not token: return None
    item = sessions_table.get_item(Key={"token": token}).get("Item")
    if not item: return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if item.get("expires_at", 0) < now_ts: return None
    # PDC-18: check session is newer than user's valid_after (set on logout)
    email = item.get("email", "")
    if email:
        try:
            user = users_table.get_item(Key={"email": email}).get("Item")
            if user:
                valid_after = int(user.get("valid_after", 0))
                session_created = int(item.get("session_created_at", 0))
                if valid_after and session_created and session_created < valid_after:
                    return None
        except Exception:
            pass  # never let this check break auth
    return item

def get_admin_auth(event) -> bool:
    if not ADMIN_SECRET: return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth    = headers.get("authorization", "").strip()
    # Require AdminSecret prefix — no raw secret fallback (PDC-10)
    for prefix in ("AdminSecret ", "adminsecret "):
        if auth.lower().startswith(prefix.lower()):
            candidate = auth[len(prefix):].strip()
            return hmac.compare_digest(candidate, ADMIN_SECRET)  # timing-safe (PDC-15)
    return False

def get_body(event) -> dict:
    try:    return json.loads(event.get("body") or "{}")
    except: return {}


# ── auth ──────────────────────────────────────────────────────────────────────

def register(body):
    email = (body.get("email") or "").strip().lower()
    pin   = str(body.get("pin") or "").strip()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return err("Invalid email address.")
    if not re.match(r'^\d{4}$', pin):
        return err("PIN must be exactly 4 digits.")
    if users_table.get_item(Key={"email": email}).get("Item"):
        return err("An account with that email already exists.", 409)
    prefs = {**DEFAULT_PREFS, "notify_email": email}
    users_table.put_item(Item={
        "email":      email,
        "pin_hash":   hash_pin(pin),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prefs":      prefs,
    })
    return ok({"message": "Account created. Please log in."}, 201)


# ── auth event logging ───────────────────────────────────────────────────────

def _get_client_ip(event: dict) -> str:
    """Extract real client IP from API Gateway event (authoritative sourceIp only).
    X-Forwarded-For is attacker-controllable and not used (PDC-12)."""
    return (event.get("requestContext", {})
                 .get("http", {})
                 .get("sourceIp", "unknown"))


def _geo_lookup(ip: str) -> dict:
    """Look up country/city for an IP using ip-api.com (free, no key needed)."""
    if not ip or ip in ("unknown", "127.0.0.1"):
        return {}
    try:
        req = urllib.request.Request(
            f"http://ip-api.com/json/{ip}?fields=country,regionName,city,status",
            headers={"User-Agent": "cartwise/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.loads(r.read())
        if d.get("status") == "success":
            return {"country": d.get("country",""), "region": d.get("regionName",""), "city": d.get("city","")}
    except Exception:
        pass
    return {}


def _log_auth_event(event: dict, email: str, success: bool, reason: str = ""):
    """Write a login attempt record to the auth_logs DynamoDB table."""
    try:
        ip  = _get_client_ip(event)
        geo = _geo_lookup(ip)
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        ua  = headers.get("user-agent", "")[:200]
        now = datetime.now(timezone.utc).isoformat()
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
        auth_logs_tbl.put_item(Item={
            "log_id":    f"{now}#{secrets.token_hex(4)}",
            "ts":        now,
            "email":     email or "(unknown)",
            "success":   success,
            "ip":        ip,
            "country":   geo.get("country", ""),
            "region":    geo.get("region", ""),
            "city":      geo.get("city", ""),
            "user_agent": ua,
            "reason":    reason,
            "expires_at": ttl,
        })
    except Exception:
        pass  # never let logging break auth


def _extract_email(addr_str: str) -> str:
    """Extract bare email from 'Name <email>' or plain email string."""
    if not addr_str:
        return ""
    m = re.search(r"<([^>]+)>", addr_str)
    if m:
        return m.group(1).strip().lower()
    return addr_str.strip().lower()


def itemName(item) -> str:
    """Normalize a prefs.items entry (str or dict) to its name string."""
    if isinstance(item, str): return item
    if isinstance(item, dict): return item.get("name", "")
    return ""


def _verify_resend_webhook(event: dict) -> bool:
    """Verify Svix HMAC signature on Resend webhook requests."""
    import base64 as _b64
    secret = RESEND_WEBHOOK_SECRET
    if not secret or secret == "not-yet-configured":
        return False
    # Svix secrets are base64-encoded with a "whsec_" prefix.
    # Strip the prefix and decode to raw bytes before using as the HMAC key.
    if secret.startswith("whsec_"):
        secret_bytes = _b64.b64decode(secret[len("whsec_"):])
    else:
        secret_bytes = secret.encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    svix_id        = headers.get("svix-id", "")
    svix_timestamp = headers.get("svix-timestamp", "")
    svix_signature = headers.get("svix-signature", "")
    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        raw_body = _b64.b64decode(raw_body).decode("utf-8")
    signed_content = f"{svix_id}.{svix_timestamp}.{raw_body}"
    expected = _b64.b64encode(
        hmac.new(secret_bytes, signed_content.encode(), hashlib.sha256).digest()
    ).decode()
    for tok in svix_signature.split(" "):
        raw_sig = tok.split(",")[-1]
        if hmac.compare_digest(expected, raw_sig):
            return True
    return False


import html.parser as _hp

def _parse_publix_list_html(html_body: str) -> list:
    """Extract item names from a Publix My List HTML email body."""
    class _P(_hp.HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = self.is_item_table = self.in_cell = self.in_th = False
            self.col_idx = self.row_count = 0
            self.cur = ""
            self._th_texts = []
            self.items = []

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.in_table = True; self.row_count = 0; self._th_texts = []
            elif tag == "tr" and self.in_table:
                self.col_idx = -1; self.row_count += 1
            elif tag in ("td", "th") and self.in_table:
                self.col_idx += 1; self.cur = ""
                self.in_cell = (tag == "td"); self.in_th = (tag == "th")

        def handle_endtag(self, tag):
            if tag == "table":
                self.in_table = self.is_item_table = False
            elif tag in ("td", "th") and self.in_table:
                txt = self.cur.strip()
                if self.in_th:
                    self._th_texts.append(txt.lower())
                    if "name" in self._th_texts and "quantity" in self._th_texts:
                        self.is_item_table = True
                elif self.in_cell and self.is_item_table:
                    if self.col_idx == 1 and self.row_count > 1 and txt:
                        self.items.append(txt)
                self.in_cell = self.in_th = False

        def handle_data(self, data):
            if self.in_cell or self.in_th:
                self.cur += data

    parser = _P()
    parser.feed(html_body)
    return parser.items


def inbound_email_list(event):
    """POST /inbound/email-list — Resend inbound webhook for list import."""
    # Step 2: Verify Svix signature before anything else
    sig_ok = _verify_resend_webhook(event)
    if not sig_ok:
        secret_set = bool(RESEND_WEBHOOK_SECRET and RESEND_WEBHOOK_SECRET != "not-yet-configured")
        _log_app_event("api", "warn", action="inbound-sig-fail",
                       secret_configured=secret_set,
                       has_svix_id=bool((event.get("headers") or {}).get("svix-id")),
                       message="Webhook signature verification failed — check RESEND_WEBHOOK_SECRET")
        return {"statusCode": 400, "headers": CORS,
                "body": json.dumps({"error": "Bad signature"})}

    body     = get_body(event)
    evt_type = body.get("type", "")
    _log_app_event("api", "info", action="inbound-webhook-received",
                   event_type=evt_type,
                   has_data=bool(body.get("data")))

    if evt_type != "email.received":
        return ok({"skipped": True, "reason": "event type not email.received"})

    data     = body.get("data", {})
    email_id = data.get("email_id", "")
    to_addrs = data.get("to", [])
    raw_from = data.get("from", "")

    # Step 3: Confirm this email is destined for our inbound address
    if INBOUND_EMAIL_ADDR:
        match = any(INBOUND_EMAIL_ADDR.lower() in a.lower() for a in to_addrs)
        if not match:
            _log_app_event("api", "warn", action="inbound-wrong-dest",
                           to_addrs=str(to_addrs)[:200],
                           expected=INBOUND_EMAIL_ADDR,
                           message="Inbound email arrived at unexpected destination address")
            return ok({"skipped": True, "reason": "not our inbound address"})

    # Step 4: Extract sender email (the user who forwarded)
    sender_email = _extract_email(raw_from)
    _log_app_event("api", "info", action="inbound-sender-extracted",
                   email_id=email_id,
                   sender=sender_email,
                   raw_from=raw_from[:100],
                   to_addrs=str(to_addrs)[:200])
    if not sender_email:
        _log_app_event("api", "warn", action="inbound-no-from",
                       raw_from=raw_from[:100],
                       message="Could not extract sender email from from: header")
        return ok({"skipped": True, "reason": "no from address"})

    # Step 5: Match user — login email first, then notify_email
    user = None
    try:
        item = users_table.get_item(Key={"email": sender_email}).get("Item")
        if item:
            user = item
            _log_app_event("api", "info", action="inbound-user-matched",
                           sender=sender_email, match_type="login_email")
    except Exception as e:
        _log_app_event("api", "error", action="inbound-user-lookup-error",
                       sender=sender_email, message=str(e)[:200])

    if not user:
        try:
            from boto3.dynamodb.conditions import Attr
            resp = users_table.scan(
                FilterExpression=Attr("prefs.notify_email").eq(sender_email)
            )
            if resp.get("Items"):
                user = resp["Items"][0]
                _log_app_event("api", "info", action="inbound-user-matched",
                               sender=sender_email, match_type="notify_email",
                               matched_user=user["email"])
            else:
                _log_app_event("api", "warn", action="inbound-no-user",
                               sender=sender_email,
                               message="No user found with this login or notification email — "
                                       "user must forward from their registered address")
        except Exception as e:
            _log_app_event("api", "error", action="inbound-notify-scan-error",
                           sender=sender_email, message=str(e)[:200])

    if not user:
        return ok({"skipped": True})

    # Step 6: Fetch full email HTML body from Resend API
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_key or not email_id:
        _log_app_event("api", "error", action="inbound-config-error",
                       has_key=bool(resend_key), has_email_id=bool(email_id))
        return {"statusCode": 500, "headers": CORS,
                "body": json.dumps({"error": "Configuration error"})}

    _log_app_event("api", "info", action="inbound-fetching-body",
                   email_id=email_id, user=user["email"])
    try:
        req = urllib.request.Request(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {resend_key}",
                     "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            email_data = json.loads(r.read())
    except Exception as e:
        _log_app_event("api", "error", action="inbound-fetch-failed",
                       email_id=email_id, user=user["email"],
                       message=str(e)[:200],
                       hint="Check RESEND_API_KEY is valid and email_id exists in Resend")
        return {"statusCode": 502, "headers": CORS,
                "body": json.dumps({"error": "Failed to fetch email content"})}

    # Step 7: Parse item names from HTML body
    html_body    = email_data.get("html") or ""
    html_len     = len(html_body)
    parsed_names = _parse_publix_list_html(html_body)
    _log_app_event("api", "info", action="inbound-html-parsed",
                   user=user["email"], email_id=email_id,
                   html_bytes=html_len,
                   items_found=len(parsed_names),
                   items_preview=str(parsed_names[:5]),
                   has_html=html_len > 0,
                   hint=("" if parsed_names else
                         "HTML received but no Publix My List table found — "
                         "verify the email contains the Name/Quantity table structure"))

    if not parsed_names:
        _log_app_event("api", "warn", action="inbound-no-items",
                       user=user["email"], html_bytes=html_len,
                       html_snippet=html_body[:300] if html_body else "(empty)",
                       message="No items parsed from HTML body")
        return ok({"imported": 0, "skipped": True, "reason": "no items found in HTML"})

    # Step 8: Merge into user's list and save
    prefs    = _to_py(user.get("prefs", DEFAULT_PREFS))
    existing = {itemName(i).lower() for i in (prefs.get("items") or [])}
    added    = 0
    skipped_names = []
    for name in parsed_names:
        if name.lower() not in existing:
            prefs.setdefault("items", []).append(name)
            existing.add(name.lower())
            added += 1
        else:
            skipped_names.append(name)

    users_table.update_item(
        Key={"email": user["email"]},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs}
    )
    _log_app_event("api", "info", action="inbound-list-imported",
                   user=user["email"], added=added,
                   total_parsed=len(parsed_names),
                   skipped_duplicates=len(skipped_names),
                   added_items=str(parsed_names[:added]),
                   skipped_items=str(skipped_names[:10]))
    return ok({"imported": added, "total_parsed": len(parsed_names)})


def admin_inbound_logs(event):
    """GET /admin/inbound-logs?sender=...&limit=N
    Returns app-log entries with inbound- actions, optionally filtered by sender."""
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        from boto3.dynamodb.conditions import Attr
        params = event.get("queryStringParameters") or {}
        limit  = min(int(params.get("limit", 200)), 500)
        sender = (params.get("sender") or "").strip().lower()

        fe = Attr("action").begins_with("inbound-")
        if sender:
            fe = fe & (Attr("sender").eq(sender) | Attr("user").eq(sender))

        resp  = app_logs_tbl.scan(FilterExpression=fe, Limit=2000)
        items = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit], "sender_filter": sender or None})
    except Exception as e:
        print(f"[CW] admin_inbound_logs: {e}")
        return err("Could not fetch inbound logs.", 500)



def _log_app_event(source: str, level: str = "info", **fields):
    """Write a structured application log entry to app_logs DynamoDB table.
    source: 'frontend' | 'api' | 'email' | 'cache'
    Never raises — logging must never break the main request flow.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
        item = {
            "log_id":     f"{now}#{secrets.token_hex(4)}",
            "ts":         now,
            "source":     source,
            "level":      level,
            "expires_at": ttl,
            **{k: v for k, v in fields.items() if v is not None},
        }
        app_logs_tbl.put_item(Item=item)
    except Exception:
        pass


LOGIN_MAX_ATTEMPTS    = 5    # failures before lockout
LOGIN_LOCKOUT_MINUTES = 60   # how long the lockout lasts
LOGIN_WINDOW_MINUTES  = 15   # rolling window for counting failures


def login(body):
    email = (body.get("email") or "").strip().lower()
    pin   = str(body.get("pin") or "").strip()
    now   = datetime.now(timezone.utc)

    user = users_table.get_item(Key={"email": email}).get("Item")

    # Timing-safe: always run verify_pin even when user not found so
    # response time cannot be used to enumerate valid email addresses.
    if not user:
        verify_pin(pin, "")
        _log_auth_event(body.get("__event__", {}), email, False, "bad_pin")
        return err("Invalid email or PIN.", 401)

    user = _to_py(user)

    # ── Check lockout ──────────────────────────────────────────────
    lockout_until_str = user.get("lockout_until", "")
    if lockout_until_str:
        try:
            lockout_until = datetime.fromisoformat(lockout_until_str)
            if lockout_until.tzinfo is None:
                lockout_until = lockout_until.replace(tzinfo=timezone.utc)
            if now < lockout_until:
                remaining_secs = int((lockout_until - now).total_seconds())
                remaining_mins = max(1, (remaining_secs + 59) // 60)
                _log_auth_event(body.get("__event__", {}), email, False, "locked_out")
                return err(
                    f"Account locked. Try again in {remaining_mins} minute{'s' if remaining_mins != 1 else ''}.",
                    429,
                    _extra={"locked_until": lockout_until.isoformat(),
                            "retry_after_seconds": remaining_secs}
                )
        except Exception:
            pass

    # ── Verify PIN ─────────────────────────────────────────────────
    pin_ok = verify_pin(pin, user.get("pin_hash", ""))

    if not pin_ok:
        attempts = int(user.get("failed_attempts", 0)) + 1
        update_expr = "SET failed_attempts = :a, last_failed_at = :t"
        expr_vals   = {":a": attempts, ":t": now.isoformat()}

        if attempts >= LOGIN_MAX_ATTEMPTS:
            lockout_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
            update_expr  += ", lockout_until = :lu"
            expr_vals[":lu"] = lockout_until.isoformat()
            users_table.update_item(
                Key={"email": email},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_vals
            )
            _log_auth_event(body.get("__event__", {}), email, False, "locked_out")
            return err(
                f"Too many failed attempts. Account locked for {LOGIN_LOCKOUT_MINUTES} minutes.",
                429,
                _extra={"locked_until": lockout_until.isoformat()}
            )

        users_table.update_item(
            Key={"email": email},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_vals
        )
        remaining = LOGIN_MAX_ATTEMPTS - attempts
        _log_auth_event(body.get("__event__", {}), email, False, "bad_pin")
        return err(
            f"Invalid email or PIN. {remaining} attempt{'s' if remaining != 1 else ''} remaining before lockout.",
            401
        )

    # ── Success — clear lockout state ──────────────────────────────
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="REMOVE failed_attempts, lockout_until, last_failed_at"
    )
    token      = secrets.token_hex(32)
    expires_at = int((now + timedelta(hours=SESSION_TTL_HOURS)).timestamp())
    sessions_table.put_item(Item={"token": token, "email": email, "expires_at": expires_at,
                                  "session_created_at": int(datetime.now(timezone.utc).timestamp())})
    _log_auth_event(body.get("__event__", {}), email, True)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    if not prefs.get("notify_email"):
        prefs["notify_email"] = email
    return ok({"token": token, "email": email, "prefs": prefs})


def logout(event):
    sess = get_session(event)
    if sess:
        # Delete this session token
        sessions_table.delete_item(Key={"token": sess.get("token", "")})
        # PDC-18: set valid_after to now so all older sessions are also rejected
        try:
            users_table.update_item(
                Key={"email": sess["email"]},
                UpdateExpression="SET valid_after = :t",
                ExpressionAttributeValues={":t": int(datetime.now(timezone.utc).timestamp())},
            )
        except Exception as e:
            print(f"[CW] logout valid_after update failed: {e}")
    return ok({"message": "Logged out."})


def change_pin(event, body):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    current_pin = str(body.get("current_pin") or "").strip()
    new_pin     = str(body.get("new_pin") or "").strip()
    if not re.match(r'^\d{4}$', new_pin): return err("New PIN must be 4 digits.")
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user or not verify_pin(current_pin, user.get("pin_hash", "")):
        return err("Current PIN is incorrect.", 401)
    users_table.update_item(Key={"email": sess["email"]},
        UpdateExpression="SET pin_hash = :p",
        ExpressionAttributeValues={":p": hash_pin(new_pin)})
    return ok({"message": "PIN updated."})


# ── user prefs ────────────────────────────────────────────────────────────────

def get_prefs(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    if not prefs.get("notify_email"): prefs["notify_email"] = sess["email"]
    return ok({"prefs": prefs, "email": sess["email"]})


def save_prefs(event, body):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    incoming = body.get("prefs", body)
    # PDC-09: filter to allowed keys only — reject arbitrary key injection
    ALLOWED_PREF_KEYS = {"store_id", "store_name", "store_address", "notify_email",
                         "email_enabled", "items", "matching", "notifications"}
    filtered = {k: v for k, v in incoming.items() if k in ALLOWED_PREF_KEYS}
    prefs    = {**DEFAULT_PREFS, **filtered}
    ne = (prefs.get("notify_email") or "").strip()
    if ne and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', ne):
        return err("Invalid notification email address.")
    if not ne: prefs["notify_email"] = sess["email"]
    # PDC-09: validate matching threshold is int in [0, 100]
    matching = prefs.get("matching") or {}
    if "threshold" in matching:
        try:
            t = int(matching["threshold"])
            matching["threshold"] = max(0, min(100, t))
        except (ValueError, TypeError):
            matching["threshold"] = 75
        prefs["matching"] = matching
    # PDC-09: cap item strings at 200 chars each, cap list at 200 items
    if isinstance(prefs.get("items"), list):
        prefs["items"] = [
            ({**i, "name": str(i.get("name", ""))[:200]} if isinstance(i, dict) else i)
            for i in prefs["items"][:200]
        ]
    users_table.update_item(Key={"email": sess["email"]},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs})
    return ok({"message": "Preferences saved.", "prefs": prefs})


def delete_account(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    users_table.delete_item(Key={"email": sess["email"]})
    sessions_table.delete_item(Key={"token": sess.get("token", "")})
    return ok({"message": "Account deleted."})


def unsubscribe(event):
    """
    GET  /user/unsubscribe?email=...&token=...
         Returns a branded HTML confirmation page (browser-friendly).
    POST /user/unsubscribe?email=...&token=...
         Sets email_enabled=False for the matching account.
         Requires no authentication — token is the only credential.
         Scope strictly limited to email_enabled on the token-owner's account.
    """
    qs     = event.get("queryStringParameters") or {}
    email  = (qs.get("email") or "").strip().lower()
    token  = (qs.get("token") or "").strip()
    method = event.get("httpMethod", event.get("requestContext", {}).get("http", {}).get("method", "GET")).upper()

    # ── validate inputs ───────────────────────────────────────────────────────
    if not email or not token:
        return _unsub_html("Invalid Link", "This unsubscribe link is missing required parameters.", ok=False)

    if not UNSUB_SECRET or UNSUB_SECRET == "changeme-set-in-deploy":
        return _unsub_html("Configuration Error", "Unsubscribe is not configured on this server.", ok=False)

    if not _verify_unsub_token(email, token):
        return _unsub_html("Invalid Token", "This unsubscribe link is not valid or has expired.", ok=False)

    # ── verify user exists ────────────────────────────────────────────────────
    item = users_table.get_item(Key={"email": email}).get("Item")
    if not item:
        return _unsub_html("Account Not Found", "No account found for this email address.", ok=False)

    # ── GET: confirmation page ────────────────────────────────────────────────
    if method == "GET":
        already_off = not _to_py(item.get("prefs", {})).get("email_enabled", True)
        if already_off:
            return _unsub_html("Already Unsubscribed",
                f"Email alerts are already turned off for <strong>{email}</strong>.")
        # Show a confirmation form so clicking the link doesn't immediately unsubscribe
        # (some email clients pre-fetch links — POST on user action only)
        return _unsub_confirm_page(email, token)

    # ── POST: perform unsubscribe ─────────────────────────────────────────────
    if method == "POST":
        prefs = _to_py(item.get("prefs", {}))
        prefs["email_enabled"] = False
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET prefs = :p",
            ExpressionAttributeValues={":p": prefs},
        )
        _log_app_event("api", "info", action="unsubscribe", email=email)
        return _unsub_html("Unsubscribed",
            f"Email alerts have been turned off for <strong>{email}</strong>. "
            f"You can re-enable them at any time by signing in to Cartwise.")

    return _unsub_html("Method Not Allowed", "Unexpected request method.", ok=False)


def _unsub_html(title: str, body_html: str, ok: bool = True) -> dict:
    """Return a browser-friendly HTML response for unsubscribe feedback pages."""
    colour  = "#1a6b3c" if ok else "#c0392b"
    icon    = "✅" if ok else "❌"
    app_url = FRONTEND_URL or "#"
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Cartwise</title>
<style>
  body{{font-family:Arial,sans-serif;background:#f5f5f5;display:flex;align-items:center;
        justify-content:center;min-height:100vh;margin:0;padding:24px;box-sizing:border-box}}
  .card{{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);
          padding:40px 36px;max-width:480px;width:100%;text-align:center}}
  h1{{color:{colour};font-size:22px;margin:16px 0 10px}}
  p{{color:#555;line-height:1.6;margin:0 0 24px}}
  .icon{{font-size:48px;line-height:1}}
  a{{color:#1a6b3c;text-decoration:none;font-weight:600}}
  a:hover{{text-decoration:underline}}
</style></head>
<body><div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{body_html}</p>
  <a href="{app_url}">← Back to Cartwise</a>
</div></body></html>"""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8",
                    "Access-Control-Allow-Origin": "*"},
        "body": html,
    }


def _unsub_confirm_page(email: str, token: str) -> dict:
    """HTML page with a POST button — prevents email client link prefetch from unsubscribing."""
    app_url    = FRONTEND_URL or "#"
    action_url = f"{API_URL}/user/unsubscribe" if API_URL else "/user/unsubscribe"
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe — Cartwise</title>
<style>
  body{{font-family:Arial,sans-serif;background:#f5f5f5;display:flex;align-items:center;
        justify-content:center;min-height:100vh;margin:0;padding:24px;box-sizing:border-box}}
  .card{{background:white;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);
          padding:40px 36px;max-width:480px;width:100%;text-align:center}}
  h1{{color:#1a6b3c;font-size:22px;margin:16px 0 10px}}
  p{{color:#555;line-height:1.6;margin:0 0 24px}}
  .icon{{font-size:48px;line-height:1}}
  .btn{{display:inline-block;background:#c0392b;color:white;padding:12px 28px;
         border-radius:6px;font-size:15px;font-weight:700;border:none;cursor:pointer;
         text-decoration:none;margin-bottom:12px}}
  .btn:hover{{background:#a93226}}
  .cancel{{display:block;color:#888;font-size:13px;text-decoration:none;margin-top:8px}}
  .cancel:hover{{color:#555}}
</style></head>
<body><div class="card">
  <div class="icon">📧</div>
  <h1>Unsubscribe from Deal Alerts?</h1>
  <p>Click the button below to stop weekly email alerts for <strong>{email}</strong>.
     You can re-enable them at any time by signing in.</p>
  <form method="POST" action="{action_url}?email={urllib.parse.quote(email)}&token={token}">
    <button class="btn" type="submit">Yes, unsubscribe me</button>
  </form>
  <a class="cancel" href="{app_url}">Cancel — keep my alerts</a>
</div></body></html>"""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8",
                    "Access-Control-Allow-Origin": "*"},
        "body": html,
    }


# ── store search ──────────────────────────────────────────────────────────────

def _parse_store_feature(f: dict) -> dict:
    """Parse a GeoJSON feature from the storelocator API into a clean store dict."""
    p    = f.get("properties") or f  # single-store endpoint returns props directly
    geo  = f.get("geometry", {}).get("coordinates", [None, None])
    addr = p.get("address") or {}
    phones = p.get("phoneNumbers") or {}
    img    = p.get("image") or {}
    hours  = p.get("hours") or []
    # Format today's hours (first entry = today)
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
                    from datetime import datetime
                    t = datetime.fromisoformat(iso)
                    return t.strftime("%-I:%M %p").replace(":00 ", " ")
                except Exception:
                    return iso[11:16] if len(iso) > 15 else iso
            hours_str = f"{fmt_time(h.get('openTime',''))} – {fmt_time(h.get('closeTime',''))}"
    street  = addr.get("streetAddress","")
    city    = addr.get("city","")
    state   = addr.get("state","")
    zipcode = addr.get("zip","")
    address_full = f"{street}, {city}, {state} {zipcode}".strip(", ")
    return {
        "id":           str(p.get("storeNumber") or ""),
        "name":         p.get("name") or "",
        "short_name":   p.get("shortName") or "",
        "street":       street,
        "city":         city,
        "state":        state,
        "zip":          zipcode,
        "address":      address_full,
        "phone":        phones.get("Store",""),
        "pharmacy_phone": phones.get("Pharmacy",""),
        "hours_today":  hours_str,
        "hours_raw":    hours,
        "lat":          geo[0] if geo else None,
        "lng":          geo[1] if geo else None,
        "img_thumb":    img.get("thumbnail",""),
        "img_hero":     img.get("hero",""),
        "distance":     p.get("distance"),
    }


# Headers required by Publix storelocator API (CORS-protected, needs Origin)
_STORE_HDRS = {
    "Accept":          "application/geo+json",
    "Accept-Encoding": "gzip, deflate",
    "Origin":          "https://www.publix.com",
    "Referer":         "https://www.publix.com/",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    params  = event.get("queryStringParameters") or {}
    query   = (params.get("q") or "").strip()
    if not query: return err("Missing ?q=")

    # Detect query type — store numbers are 3-4 digits; 5-digit inputs are always ZIPs
    is_store_num = bool(re.match(r'^\d{3,4}$', query))
    is_zip       = bool(re.match(r'^\d{5}$', query))

    if is_store_num:
        # Single store by number — returns GeoJSON with geo+json Accept header
        url = (f"https://services.publix.com/storelocator/api/v1/stores/"
               f"?types=R,G,H,N,S&count=1&distance=1000&includeOpenAndCloseDates=true"
               f"&storeNumber={urllib.parse.quote(query)}&includeStore=true&isWebsite=true")
        try:
            raw  = _store_fetch(url)
            features = raw.get("features", [])
            stores = [_parse_store_feature(f) for f in features if f.get("properties",{}).get("storeNumber")]
            return ok({"stores": stores[:1]})
        except Exception as e:
            print(f"[CW] search_stores lookup: {e}")
        return err("Store lookup failed.", 502)
    else:
        param = "zip" if is_zip else "city"
        url = (f"https://services.publix.com/storelocator/api/v1/stores/"
               f"?types=R,G,H,N,S&count=10&distance=50&includeOpenAndCloseDates=true"
               f"&{param}={urllib.parse.quote(query)}&isWebsite=true")
        try:
            raw = _store_fetch(url)
        except Exception as e:
            print(f"[CW] search_stores search: {e}")
        return err("Store search failed.", 502)
        features = raw.get("features", [])
        stores = [_parse_store_feature(f) for f in features if f.get("properties",{}).get("storeNumber")]
        return ok({"stores": stores[:10]})


# ── deals ─────────────────────────────────────────────────────────────────────

def _enrich_deal(d: dict) -> dict:
    """Add computed boolean fields to a deal dict (works on both cached and raw API dicts)."""
    saving_type = (d.get("saving_type") or d.get("savingType") or "WeeklyAd")
    brand_lower = (d.get("brand") or "").lower()
    title_lower = (d.get("title") or "").lower()
    savings_str = (d.get("savings") or "").lower()
    categories  = d.get("categories") or []
    is_publix   = brand_lower == "publix" or title_lower.startswith("publix ")
    has_coupon  = bool(d.get("has_coupon") or d.get("hasCoupon")) or saving_type in ("PrintableCoupon", "DigitalCoupon")
    is_stacked  = saving_type == "StackedDeals"
    is_extra    = saving_type == "ExtraSavings"
    import re as _re
    _bogo_text = title_lower + " " + savings_str
    is_bogo     = (
        "bogo" in categories
        or bool(_re.search(r"b[12]g1|buy \d.{0,8}get \d|buy one.{0,10}get one", _bogo_text))
    )
    return {**d,
        "saving_type":     saving_type,
        "is_bogo":         is_bogo,
        "categories":      categories,
        "is_publix_brand": is_publix,
        "has_coupon":      has_coupon,
        "is_stacked":      is_stacked,
        "is_extra":        is_extra,
    }


def _parse_deal(d: dict) -> dict:
    """Re-enrich a deal that was previously serialised to the DynamoDB cache."""
    return _enrich_deal(d)


def _parse_deals_raw(raw: dict) -> list[dict]:
    """Parse deals straight from the live Publix savings API response."""
    deals = []
    for d in (raw.get("Savings") or []):
        saving_type = (d.get("savingType") or "WeeklyAd")
        base = {
            "id":          str(d.get("id", "")),
            "title":       d.get("title", ""),
            "description": d.get("description", ""),
            "savings":     d.get("savings", ""),
            "save_line":   d.get("additionalDealInfo", ""),
            "fine_print":  d.get("finePrint") or "",
            "brand":       d.get("brand", ""),
            "department":  d.get("department", ""),
            "valid_from":  d.get("wa_startDateFormatted", ""),
            "valid_thru":  d.get("wa_endDateFormatted", ""),
            "image_url":   d.get("enhancedImageUrl") or d.get("imageUrl") or "",
            "saving_type": saving_type,
            "coupon_id":   str(d.get("dcId")) if d.get("dcId") else "",
            "categories":  d.get("categories") or [],
        }
        deals.append(_enrich_deal(base))
    return deals


# Keep old name as alias so the scraper lambda (which imports nothing from here) is unaffected
def _parse_deals(raw: dict) -> list[dict]:
    return _parse_deals_raw(raw)


def get_deals(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    if not store_id: return err("Missing store_id parameter.")

    # Prefer the DynamoDB cache written by the weekly scraper
    deals_table_obj = dynamodb.Table(os.environ["DEALS_TABLE"])
    cached = deals_table_obj.get_item(Key={"store_id": store_id}).get("Item")
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
            # Legacy single-row format
            raw_deals = json.loads(cached.get("deals", "[]"))
        deals = [_parse_deal(d) for d in raw_deals]
        _log_app_event("cache", "info", hit=True,  store_id=store_id, endpoint="/deals", deal_count=len(deals))
    else:
        # No cache yet — fall back to a live fetch so first-time users see data
        _hdrs = {
            "Accept":      "application/json, text/plain, */*",
            "Origin":      "https://www.publix.com",
            "Referer":     "https://www.publix.com/",
            "publixstore": str(store_id),
            "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        deals = []
        seen_ids = set()
        updated_at = ""
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
                if not deals:
                    _log_app_event("cache", "warn", hit=False, store_id=store_id, endpoint="/deals",
                                   message=f"Live fetch failed: {e}")
                    print(f"[CW] get_deals live fetch: {e}")
        return err("Deals temporarily unavailable.", 502)

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

    # Unique saving_type values present in this week's data (drives dynamic Savings filter)
    saving_types = sorted({d["saving_type"] for d in deals if d.get("saving_type")})

    return ok({
        "deals":        deals,
        "total":        len(deals),
        "store_id":     store_id,
        "updated_at":   updated_at,
        "counts":       counts,
        "dept_counts":  dept_counts,
        "saving_types": saving_types,
    })



# ── admin: scrape logs ────────────────────────────────────────────────────────

def get_scrape_logs(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        resp  = scrape_logs_tbl.scan(Limit=50)
        items = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        return ok({"logs": items[:15]})
    except Exception as e:
        print(f"[CW] admin_scrape_logs: {e}")
        return err("Could not fetch scrape logs.", 500)


def invoke_scraper(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        lambda_client.invoke(FunctionName=SCRAPER_FUNCTION, InvocationType="Event")
        return ok({"message": f"Scraper invoked. Check logs in ~30 seconds."})
    except Exception as e:
        print(f"[CW] admin_scrape_now: {e}")
        return err("Failed to invoke scraper.", 500)


def get_log_tail(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
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
                ts  = datetime.fromtimestamp(e["timestamp"]/1000, tz=timezone.utc)
                all_lines.append({"ts": ts.isoformat(), "msg": e["message"].rstrip("\n")})

        all_lines.sort(key=lambda x: x["ts"])
        return ok({"lines": all_lines[-150:], "stream": streams[0]["logStreamName"]})
    except Exception as e:
        print(f"[CW] admin_logs_tail: {e}")
        return err("Could not fetch logs.", 500)


# ── admin: user management ────────────────────────────────────────────────────

def admin_list_users(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        items, resp = [], users_table.scan()
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = users_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        users = []
        for u in _to_py(items):
            if u.get("email") == "__meta__": continue
            p = u.get("prefs") or {}
            users.append({
                "email":        u["email"],
                "created_at":   u.get("created_at", ""),
                "store_id":     p.get("store_id", ""),
                "store_name":   p.get("store_name", ""),
                "notify_email": p.get("notify_email") or u["email"],
                "item_count":   len(p.get("items") or []),
                "items":        p.get("items") or [],
                "threshold":    (p.get("matching") or {}).get("threshold", 75),
            })
        users.sort(key=lambda x: x["email"])
        return ok({"users": users, "total": len(users)})
    except Exception as e:
        print(f"[CW] admin_list_users: {e}")
        return err("Failed to list users.", 500)


def admin_get_user(event, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    safe = _to_py(user)
    safe.pop("pin_hash", None)
    return ok({"user": safe})


def admin_create_user(event, body):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    return register(body)


def admin_delete_user(event, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    if not email: return err("Missing email.")
    users_table.delete_item(Key={"email": email})
    # Track deletion count in metadata record
    try:
        users_table.update_item(
            Key={"email": "__meta__"},
            UpdateExpression="ADD deleted_count :one SET last_deleted_at = :ts",
            ExpressionAttributeValues={":one": 1, ":ts": datetime.now(timezone.utc).isoformat()}
        )
    except Exception:
        pass
    return ok({"message": f"User {email} deleted."})


def admin_reset_pin(event, body, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    new_pin = str(body.get("new_pin") or "").strip()
    if not re.match(r'^\d{4}$', new_pin): return err("New PIN must be 4 digits.")
    if not users_table.get_item(Key={"email": email}).get("Item"):
        return err("User not found.", 404)
    users_table.update_item(Key={"email": email},
        UpdateExpression="SET pin_hash = :p",
        ExpressionAttributeValues={":p": hash_pin(new_pin)})
    return ok({"message": f"PIN reset for {email}."})


def admin_reset_email(event, body, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    new_email = (body.get("new_email") or "").strip().lower()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', new_email):
        return err("Invalid email address.")
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    # PDC-08: check target doesn't already exist to prevent silent overwrite
    if users_table.get_item(Key={"email": new_email}).get("Item"):
        return err("An account with that email already exists.", 409)
    # Create new record with updated email (and update notify_email if it matched)
    new_item = {**user, "email": new_email}
    prefs = _to_py(new_item.get("prefs") or {})
    if prefs.get("notify_email", "").lower() == email.lower():
        prefs["notify_email"] = new_email
        new_item["prefs"] = prefs
    users_table.put_item(Item=new_item)
    users_table.delete_item(Key={"email": email})
    return ok({"message": f"Email changed from {email} to {new_email}."})


def admin_patch_prefs(event, body, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    existing = _to_py(user.get("prefs", DEFAULT_PREFS))
    merged   = {**existing, **(body.get("prefs", body))}
    users_table.update_item(Key={"email": email},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": merged})
    return ok({"message": f"Prefs updated for {email}.", "prefs": merged})


def admin_clear_items(event, email: str):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    user = users_table.get_item(Key={"email": email}).get("Item")
    if not user: return err("User not found.", 404)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    prefs["items"] = []
    users_table.update_item(Key={"email": email},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs})
    return ok({"message": f"Items cleared for {email}."})



def admin_stats(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)

    # Scan all users
    items, resp = [], users_table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = users_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    users = [u for u in _to_py(items) if u.get("email") != "__meta__"]

    total = len(users)

    # Users by join date (by month)
    from collections import Counter, defaultdict
    by_month = Counter()
    for u in users:
        ts = u.get("created_at", "")
        if ts:
            by_month[ts[:7]] += 1  # "YYYY-MM"

    # Geography: group by store city/name
    by_store = Counter()
    for u in users:
        p = u.get("prefs") or {}
        name = p.get("store_name") or ""
        sid  = p.get("store_id") or ""
        if name:
            # Extract city: store names are like "Publix Super Market at Shoppes at XYZ, Asheville, NC"
            # Try to get last part after last comma as city
            parts = [x.strip() for x in name.split(",")]
            city = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else sid or "Unknown")
            by_store[city] += 1
        elif sid:
            by_store[f"Store #{sid}"] += 1
        else:
            by_store["No store set"] += 1

    # Average list items
    item_counts = [len((u.get("prefs") or {}).get("items") or []) for u in users]
    avg_items = round(sum(item_counts) / total, 1) if total else 0

    # Popular items across all users
    all_items = Counter()
    for u in users:
        p = u.get("prefs") or {}
        for item in (p.get("items") or []):
            if item and isinstance(item, str):
                all_items[item.strip().lower()] += 1

    popular = [{"item": k, "count": v} for k, v in all_items.most_common(25)]

    # Users with no items
    no_items = sum(1 for c in item_counts if c == 0)

    # Fetch deletion metadata
    meta = users_table.get_item(Key={"email": "__meta__"}).get("Item") or {}
    meta = _to_py(meta)
    deleted_count = int(meta.get("deleted_count", 0))

    return ok({
        "total_users":     total,
        "deleted_users":   deleted_count,
        "avg_items":       avg_items,
        "no_items_count":  no_items,
        "by_month":        [{"month": k, "count": v} for k, v in sorted(by_month.items())],
        "by_geography":    [{"location": k, "count": v} for k, v in by_store.most_common(20)],
        "popular_items":   popular,
    })


# ── admin: auth logs ─────────────────────────────────────────────────────────

def get_auth_logs(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        params  = event.get("queryStringParameters") or {}
        limit   = min(int(params.get("limit", 100)), 500)
        resp    = auth_logs_tbl.scan(Limit=limit)
        items   = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit]})
    except Exception as e:
        print(f"[CW] admin_auth_logs: {e}")
        return err("Could not fetch auth logs.", 500)


# ── admin: app/api logs ───────────────────────────────────────────────────────

def get_app_logs(event):
    if not get_admin_auth(event): return err("Unauthorized.", 401)
    try:
        params  = event.get("queryStringParameters") or {}
        limit   = min(int(params.get("limit", 100)), 500)
        resp    = app_logs_tbl.scan(Limit=limit)
        items   = _to_py(resp.get("Items", []))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return ok({"logs": items[:limit]})
    except Exception as e:
        print(f"[CW] admin_app_logs: {e}")
        return err("Could not fetch app logs.", 500)


_VALID_LOG_LEVELS = {"error", "warn", "info"}

def log_frontend_error(event, body):
    """POST /log/error — receive frontend JS error reports (requires session auth).
    PDC-11: auth required to prevent log spam; level validated against allowlist."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    level = body.get("level", "error")
    if level not in _VALID_LOG_LEVELS:
        level = "error"
    try:
        ip  = _get_client_ip(event)
        now = datetime.now(timezone.utc).isoformat()
        app_logs_tbl.put_item(Item={
            "log_id":  f"{now}#{secrets.token_hex(4)}",
            "ts":      now,
            "source":  "frontend",
            "level":   level,
            "message": str(body.get("message", ""))[:500],
            "stack":   str(body.get("stack", ""))[:1000],
            "url":     str(body.get("url", ""))[:200],
            "ip":      ip,
            "user":    sess.get("email", ""),
        })
        return ok({"message": "Logged."})
    except Exception as e:
        print(f"[CW] log_frontend_error write failed: {e}")
        return err("Log write failed.", 500)


def get_deal_history(event):
    """GET /deals/history?store_id=X
    Returns all available weekly snapshots for a store, assembled from chunks,
    sorted newest-first. Payload is lightweight (no images/fine_print).
    Also returns num_weeks so the frontend can gate badge rendering.
    """
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)

    params   = event.get("queryStringParameters") or {}
    store_id = (params.get("store_id") or "").strip()
    if not store_id: return err("Missing store_id parameter.")

    try:
        resp  = history_tbl.scan(
            FilterExpression="begins_with(store_id, :prefix)",
            ExpressionAttributeValues={":prefix": f"{store_id}#"},
        )
        rows  = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = history_tbl.scan(
                FilterExpression="begins_with(store_id, :prefix)",
                ExpressionAttributeValues={":prefix": f"{store_id}#"},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            rows.extend(resp.get("Items", []))

        rows = _to_py(rows)

        index_rows = []
        chunk_map  = {}  # (store#date) -> {chunk_index: [deals]}

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

            deals = []
            for i in range(num_chunks):
                deals.extend(chunks.get(i, []))

            snapshots.append({
                "week":  week,
                "count": len(deals),
                "deals": deals,
            })

        snapshots.sort(key=lambda s: s["week"], reverse=True)
        num_weeks = len(snapshots)

        return ok({
            "store_id":  store_id,
            "num_weeks": num_weeks,
            "snapshots": snapshots,
        })

    except Exception as e:
        print(f"[CW] get_deal_history: {e}")
        return err("Could not fetch deal history.", 500)


def get_deal_corpus(event):
    """GET /deals/corpus
    Returns the global deduplicated set of all deal titles/brands ever seen.
    Used by the List tab autocomplete. Cached aggressively by the client.
    """
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)

    try:
        row = corpus_tbl.get_item(Key={"corpus_id": "global"}).get("Item")
        if not row:
            titles = []
        else:
            raw    = row.get("titles") or set()
            titles = sorted(raw)

        resp = ok({"titles": titles, "count": len(titles)})
        resp["headers"] = {**resp.get("headers", {}), "Cache-Control": "max-age=86400"}
        return resp

    except Exception as e:
        print(f"[CW] get_deal_corpus: {e}")
        return err("Could not fetch corpus.", 500)


# ── router ────────────────────────────────────────────────────────────────────

def handler(event, context):
    http_ctx = event.get("requestContext", {}).get("http", {})
    method   = http_ctx.get("method") or event.get("httpMethod", "GET")
    path     = http_ctx.get("path")   or event.get("rawPath") or event.get("path", "")
    body     = get_body(event)

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS, "body": ""}

    # User routes
    if path == "/auth/register"   and method == "POST":   return register(body)
    if path == "/auth/login"      and method == "POST":
        body["__event__"] = event
        return login(body)
    if path == "/auth/logout"     and method == "POST":   return logout(event)
    if path == "/auth/change-pin" and method == "POST":   return change_pin(event, body)
    if path == "/user/prefs"      and method == "GET":    return get_prefs(event)
    if path == "/user/prefs"      and method == "PUT":    return save_prefs(event, body)
    if path == "/user/account"    and method == "DELETE": return delete_account(event)
    if path == "/user/unsubscribe" and method in ("GET", "POST"): return unsubscribe(event)
    if path == "/inbound/email-list" and method == "POST":        return inbound_email_list(event)
    if path == "/admin/inbound-logs"  and method == "GET":         return admin_inbound_logs(event)
    if path == "/user/test-email" and method == "POST":   return send_test_email(event)
    if path == "/stores/search"   and method == "GET":    return search_stores(event)
    if path == "/deals"           and method == "GET":    return get_deals(event)
    if path == "/deals/history"   and method == "GET":    return get_deal_history(event)
    if path == "/deals/corpus"    and method == "GET":    return get_deal_corpus(event)

    # Admin fixed routes
    if path == "/admin/users"       and method == "GET":  return admin_list_users(event)
    if path == "/admin/users"       and method == "POST": return admin_create_user(event, body)
    if path == "/admin/scrape-logs" and method == "GET":  return get_scrape_logs(event)
    if path == "/admin/scrape-now"  and method == "POST": return invoke_scraper(event)
    if path == "/admin/logs/tail"   and method == "GET":  return get_log_tail(event)
    if path == "/admin/stats"        and method == "GET":  return admin_stats(event)
    if path == "/admin/auth-logs"    and method == "GET":  return get_auth_logs(event)
    if path == "/admin/app-logs"     and method == "GET":  return get_app_logs(event)
    if path == "/log/error"          and method == "POST": return log_frontend_error(event, body)

    # Admin parameterised routes  /admin/users/{email}[/action]
    if path.startswith("/admin/users/"):
        rest   = path[len("/admin/users/"):]
        parts  = rest.split("/", 1)
        uemail = urllib.parse.unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        if not action and method == "GET":    return admin_get_user(event, uemail)
        if not action and method == "DELETE": return admin_delete_user(event, uemail)
        if action == "reset-pin"  and method == "POST": return admin_reset_pin(event, body, uemail)
        if action == "reset-email"and method == "POST": return admin_reset_email(event, body, uemail)
        if action == "prefs"      and method == "PUT":  return admin_patch_prefs(event, body, uemail)
        if action == "items"      and method == "DELETE": return admin_clear_items(event, uemail)

    return err("Not found.", 404)


# ── test email ────────────────────────────────────────────────────────────────

def send_test_email(event):
    """POST /user/test-email — sends a test alert to the user's notify_email."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)
    prefs        = _to_py(user.get("prefs", DEFAULT_PREFS))
    notify_email = (prefs.get("notify_email") or sess["email"]).strip()

    resend_key  = os.environ.get("RESEND_API_KEY","")
    resend_from = (f"{os.environ.get('RESEND_FROM_NAME','Cartwise Alerts')} "
                   f"<{os.environ.get('RESEND_FROM_ADDR','onboarding@resend.dev')}>")
    if not resend_key:
        return err("Resend API key not configured.", 500)

    try:
        import resend as _resend
        _resend.api_key = resend_key
        _resend.Emails.send({
            "from":    resend_from,
            "to":      [notify_email],
            "subject": "✅ Cartwise — test email",
            "html":    f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:40px auto;color:#333;">
  <div style="background:#1a6b3c;color:white;padding:24px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;">✅ Test Email</h2>
    <p style="margin:4px 0 0;opacity:.8;">Cartwise notifications are working.</p>
  </div>
  <div style="background:white;border:1px solid #ddd;border-top:none;padding:24px;border-radius:0 0 8px 8px;">
    <p>This is a test message sent to <strong>{notify_email}</strong>.</p>
    <p style="color:#888;font-size:13px;margin-top:16px;">Sent from your Cartwise account: {sess["email"]}</p>
  </div>
</body></html>""",
        })
        _log_app_event("email", "info", ok=True,
                       to=notify_email, subject="test-email",
                       trigger="manual", user=sess["email"])
        return ok({"message": f"Test email sent to {notify_email}."})
    except Exception as e:
        _log_app_event("email", "error", ok=False,
                       to=notify_email, subject="test-email",
                       trigger="manual", user=sess["email"], message=str(e)[:200])
        print(f"[CW] send_test_email: {e}")
        return err("Failed to send email.", 500)
