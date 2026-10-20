"""lambda/helpers.py — Shared utilities, DynamoDB table references, and constants.

Imported by all other lambda modules. Contains no business logic.
"""
import json
import os
import hashlib
import hmac
import secrets
import re
import urllib.parse
import boto3
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# ── AWS clients ───────────────────────────────────────────────────────────────

_region       = os.environ.get("PDC_REGION", "us-east-1")
dynamodb      = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda", region_name=_region)
logs_client   = boto3.client("logs",   region_name=_region)

users_table         = dynamodb.Table(os.environ["USERS_TABLE"])
sessions_table      = dynamodb.Table(os.environ["SESSIONS_TABLE"])
admin_sessions_tbl  = dynamodb.Table(os.environ.get("ADMIN_SESSIONS_TABLE", "cartwise-admin-sessions"))
scrape_logs_tbl     = dynamodb.Table(os.environ["SCRAPE_LOGS_TABLE"])
auth_logs_tbl       = dynamodb.Table(os.environ["AUTH_LOGS_TABLE"])
app_logs_tbl        = dynamodb.Table(os.environ["APP_LOGS_TABLE"])
history_tbl         = dynamodb.Table(os.environ.get("HISTORY_TABLE",  "cartwise-deal-history"))
corpus_tbl          = dynamodb.Table(os.environ.get("CORPUS_TABLE",   "cartwise-deal-corpus"))

# ── Configuration ─────────────────────────────────────────────────────────────

ADMIN_SECRET     = (os.environ.get("ADMIN_SECRET", "") or "").strip()
SCRAPER_FUNCTION = os.environ.get("SCRAPER_FUNCTION", "cartwise-scraper")
LOG_GROUP        = f"/aws/lambda/{SCRAPER_FUNCTION}"
# v9.5: no default. Empty means unsubscribe links are neither issued nor accepted
# (fail closed); deploy.sh always sets it.
UNSUB_SECRET     = (os.environ.get("UNSUB_SECRET", "") or "").strip()
if UNSUB_SECRET == "changeme-set-in-deploy":
    UNSUB_SECRET = ""
# ASCII-only email validation — RFC 5321 printable characters, no Unicode.
# Local part: alphanumeric + !#$%&'*+/=?^_`{|}~.- 
# Domain: alphanumeric + hyphen + dot, TLD >= 2 chars, no leading/trailing hyphens.
EMAIL_RE = r'^[a-zA-Z0-9!#$%&\'*+/=?^_`{|}~.-]+@[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$'

# PDC-21: Common/weak PINs rejected at registration and PIN change.
# Top-20 most common 4-digit PINs (DataGenetics research).
PIN_DENYLIST = frozenset({
    "1234", "0000", "1111", "2222", "3333", "4444", "5555", "6666",
    "7777", "8888", "9999", "1212", "6969", "1122", "1004", "2000",
    "2580", "0852", "1010", "0123",
})

FRONTEND_URL     = (os.environ.get("FRONTEND_URL", "") or "").strip()

# Build the set of allowed origins from FRONTEND_URL.
# Accepts both apex (cartwise.shopping) and www subdomain automatically.
def _allowed_origins(base_url: str) -> set:
    if not base_url:
        return set()
    origins = {base_url.rstrip("/")}
    # If apex, also allow www. and vice versa
    if base_url.startswith("https://www."):
        origins.add("https://" + base_url[len("https://www."):].rstrip("/"))
    elif base_url.startswith("https://") and "//" not in base_url[8:]:
        origins.add("https://www." + base_url[len("https://"):].rstrip("/"))
    return origins

_ALLOWED_ORIGINS = _allowed_origins(FRONTEND_URL)


def cors_headers(event=None) -> dict:
    """Return CORS headers with the correct Allow-Origin for the request origin."""
    origin = ""
    if event:
        hdrs = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        origin = hdrs.get("origin", "")
    if origin and origin in _ALLOWED_ORIGINS:
        allow_origin = origin
    elif _ALLOWED_ORIGINS:
        allow_origin = FRONTEND_URL  # default to primary
    else:
        allow_origin = "*"
    return {
        "Access-Control-Allow-Origin":      allow_origin,
        "Access-Control-Allow-Headers":     "Content-Type,Authorization,Cookie",
        "Access-Control-Allow-Methods":     "GET,POST,PUT,DELETE,OPTIONS",
        "Access-Control-Allow-Credentials": "true" if allow_origin != "*" else "false",
        "Vary":                             "Origin",
        "Content-Type": "application/json",
    }
API_URL          = (os.environ.get("API_URL", "") or "").strip()
INBOUND_EMAIL_ADDR    = (os.environ.get("INBOUND_EMAIL_ADDR", "") or "").strip()
RESEND_WEBHOOK_SECRET = (os.environ.get("RESEND_WEBHOOK_SECRET", "") or "").strip()

SESSION_TTL_HOURS       = 72

# Cookie settings are per environment (set by deploy.sh). Staging uses different
# cookie names so logging into staging never overwrites the production session:
# both environments share the cartwise.shopping parent domain.
SESSION_COOKIE_NAME = (os.environ.get("SESSION_COOKIE_NAME") or "session").strip()
ADMIN_COOKIE_NAME   = (os.environ.get("ADMIN_COOKIE_NAME") or "admin_session").strip()
COOKIE_DOMAIN       = os.environ.get("COOKIE_DOMAIN", "cartwise.shopping").strip()


def make_cookie(name: str, value: str, max_age: int) -> str:
    """Build a Set-Cookie value. HttpOnly + Secure + SameSite=Lax; Domain only when configured."""
    domain = f"Domain={COOKIE_DOMAIN}; " if COOKIE_DOMAIN else ""
    return f"{name}={value}; HttpOnly; Secure; SameSite=Lax; {domain}Path=/; Max-Age={max_age}"


def read_cookie(event, name: str) -> str:
    """Return a cookie value from an API Gateway event (payload 2.0 list, or 1.0 Cookie header)."""
    prefix = name + "="
    for part in (event.get("cookies") or []):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):].strip()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    for part in headers.get("cookie", "").split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix):].strip()
    return ""
ADMIN_SESSION_TTL_HOURS = 12   # PDC-04: admin sessions expire after 12 hours
LOGIN_MAX_ATTEMPTS      = 5
LOGIN_LOCKOUT_MINUTES   = 60


# ── Rate limiter (PDC-21) ────────────────────────────────────────────────────

def check_rate_limit(scope: str, key: str, max_per_hour: int) -> bool:
    """Return True if within rate limit, False if blocked.
    Uses atomic DynamoDB counter with hourly reset. Never raises.
    scope: 'register', 'resend-verify', 'test-email', etc.
    key:   the value being throttled (IP address, email, etc.).
    """
    try:
        now       = datetime.now(timezone.utc)
        hour_key  = now.strftime("%Y%m%d-%H")
        ddb_key   = f"rate#{scope}#{key}#{hour_key}"
        expires   = int((now.replace(minute=0, second=0, microsecond=0)
                         + timedelta(hours=2)).timestamp())
        resp = app_logs_tbl.update_item(
            Key={"log_id": ddb_key},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :exp), "
                             "#src = if_not_exists(#src, :src)",
            ExpressionAttributeNames={"#c": "count", "#ttl": "expires_at",
                                      "#src": "source"},
            ExpressionAttributeValues={":one": 1, ":exp": expires, ":src": "rate-limit"},
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"].get("count", 1))
        return count <= max_per_hour
    except Exception:
        return True  # fail open — don't block legitimate users on DDB error


# Static fallback used by ok()/err() when no event is available
CORS = cors_headers(None)


def ok(body, status=200, _event=None, _headers=None):
    hdrs = cors_headers(_event) if _event else CORS
    if _headers:
        hdrs = {**hdrs, **_headers}
    return {"statusCode": status, "headers": hdrs, "body": json.dumps(body, cls=_Enc)}

# v9.8.4: the request being handled, so err() can log which endpoint failed.
# A Lambda container handles one request at a time, so a module-level dict is safe.
_REQUEST = {"method": "", "path": ""}


def set_request_context(method: str, path: str):
    _REQUEST["method"], _REQUEST["path"] = (method or "")[:10], (path or "")[:200]


def err(msg, status=400, _extra=None, _event=None):
    if status >= 400 and status not in (401, 404, 429):
        try:
            from logging_utils import _log_app_event
            _log_app_event("api", "error" if status >= 500 else "warn",
                           status=status, message=str(msg)[:300],
                           method=_REQUEST["method"] or None, path=_REQUEST["path"] or None)
        except Exception as e:
            print(f"[CW] err logging failed: {e}")
    body = {"error": msg}
    if _extra:
        body.update(_extra)
    hdrs = cors_headers(_event) if _event else CORS
    return {"statusCode": status, "headers": hdrs, "body": json.dumps(body)}


DEFAULT_PREFS = {
    "store_id":      "",
    "store_name":    "",
    "store_address": "",
    "notify_email":  "",
    "email_enabled": True,
    "items":         [],
    "matching":      {"sensitivity": "normal"},
    "notifications": {"only_matches": True},
    "bogo_mode":     "auto",      # v9.8: "auto" (from store state) | "half" | "two"
}

ALLOWED_PREF_KEYS = {
    "store_id", "store_name", "store_address", "notify_email",
    "email_enabled", "items", "matching", "notifications", "bogo_mode",
}

# ── JSON encoder ──────────────────────────────────────────────────────────────

class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)

# ── Data helpers ──────────────────────────────────────────────────────────────

def _to_py(obj):
    """Recursively convert DynamoDB Decimal types to int/float."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):  return {k: _to_py(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_to_py(i) for i in obj]
    return obj

def get_body(event) -> dict:
    try:    return json.loads(event.get("body") or "{}")
    except Exception: return {}

def itemName(item) -> str:
    """Extract the display name from a list item (dict or plain string)."""
    if isinstance(item, dict): return item.get("name", "")
    return str(item)

# ── IP extraction ─────────────────────────────────────────────────────────────

def _get_client_ip(event: dict) -> str:
    """Extract real client IP from API Gateway event (authoritative sourceIp only).
    X-Forwarded-For is attacker-controllable and not used (PDC-12)."""
    return (event.get("requestContext", {})
                 .get("http", {})
                 .get("sourceIp", "unknown"))

# ── PIN hashing (scrypt) ──────────────────────────────────────────────────────

def hash_pin(pin: str, salt: bytes = None) -> str:
    """Hash a PIN with scrypt + random salt.
    Returns 'scrypt$<hex_salt>$<hex_hash>'."""
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"

def verify_pin(pin: str, stored_hash: str) -> bool:
    """Verify a PIN against a stored scrypt hash.
    Legacy unsalted SHA-256 hashes are rejected (force re-registration)."""
    if not stored_hash or "$" not in stored_hash:
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

def _unsub_token(email: str) -> str:
    if not UNSUB_SECRET:
        raise RuntimeError("UNSUB_SECRET is not configured")
    return hmac.new(
        UNSUB_SECRET.encode(),
        email.lower().strip().encode(),
        hashlib.sha256,
    ).hexdigest()

def _verify_unsub_token(email: str, token: str) -> bool:
    if not UNSUB_SECRET:
        return False
    expected = _unsub_token(email)
    return hmac.compare_digest(expected, token.lower().strip())

# ── Session & admin auth ──────────────────────────────────────────────────────

def get_session(event):
    """Validate session token; reads HttpOnly cookie first, Bearer header as fallback.
    PDC-05: HttpOnly cookie is the primary transport for session tokens.
    HTTP API payload format 2.0 puts cookies in event['cookies'] list, not headers.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # PDC-05: prefer the HttpOnly cookie (name is per environment) over the Authorization header.
    token = read_cookie(event, SESSION_COOKIE_NAME)

    # Fallback: Bearer token in Authorization header
    if not token:
        token = headers.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()

    if not token: return None
    item = sessions_table.get_item(Key={"token": token}).get("Item")
    if not item: return None
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if item.get("expires_at", 0) < now_ts: return None
    # PDC-18: reject sessions issued before user's valid_after timestamp (set on logout)
    email = item.get("email", "")
    if email and email != "__admin__":
        try:
            user = users_table.get_item(Key={"email": email}).get("Item")
            # v9.5: a session whose account no longer exists (deleted, or the email was
            # changed by an admin) is dead. Before, it kept working until it expired (72 h).
            if not user:
                return None
            valid_after      = int(user.get("valid_after", 0))
            session_created  = int(item.get("session_created_at", 0))
            if valid_after and session_created and session_created < valid_after:
                return None
        except Exception:
            pass  # a DynamoDB hiccup must not log everyone out
    return item


def _scan_all(table, **kwargs) -> list:
    """Every item of a table (or a filtered subset), following pagination.

    v9.5: DynamoDB `Scan(Limit=N)` caps items *examined* in hash order, so a
    single limited page is an arbitrary sample, not the newest rows. The log
    tables are bounded by TTL, so reading them fully is cheap.
    """
    items, resp = [], table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    return items


def revoke_sessions(email: str) -> int:
    """Delete every session for an account (v9.5). Sessions are keyed by token with
    no email index, so this scans the (TTL-bounded) sessions table."""
    from boto3.dynamodb.conditions import Attr
    n = 0
    try:
        for item in _scan_all(sessions_table, FilterExpression=Attr("email").eq(email),
                              ProjectionExpression="#t", ExpressionAttributeNames={"#t": "token"}):
            sessions_table.delete_item(Key={"token": item["token"]})
            n += 1
    except Exception as e:
        print(f"[CW] revoke_sessions({email}) failed: {e}")
    return n


def count_deleted_user():
    """Bump the "Users Deleted" admin stat (v9.5: self-deletes and cleanup count too)."""
    try:
        users_table.update_item(
            Key={"email": "__meta__"},
            UpdateExpression="ADD deleted_count :one SET last_deleted_at = :ts",
            ExpressionAttributeValues={":one": 1, ":ts": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as e:
        print(f"[CW] count_deleted_user: {e}")


def admin_secret_fp() -> str:
    """Short fingerprint of the current ADMIN_SECRET (v9.5). Admin sessions record it at
    login, so rotating the secret ends every admin session made with the old one."""
    return hashlib.sha256(("cw-admin:" + ADMIN_SECRET).encode()).hexdigest()[:16] if ADMIN_SECRET else ""


def get_admin_auth(event) -> bool:
    """Return True if the request carries a valid admin session token (PDC-04).
    PDC-21: reads HttpOnly cookie (ADMIN_COOKIE_NAME) first, AdminToken header as fallback.
    PDC-21: raw AdminSecret fallback removed — use POST /admin/login to get a token.
    """
    if not ADMIN_SECRET: return False

    # PDC-21: prefer the HttpOnly cookie (name is per environment)
    token = read_cookie(event, ADMIN_COOKIE_NAME)

    # Fallback: AdminToken header (transition period)
    if not token:
        headers_raw = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        auth = headers_raw.get("authorization", "").strip()
        if auth.lower().startswith("admintoken "):
            token = auth[len("admintoken "):].strip()

    if not token:
        return False

    try:
        item = admin_sessions_tbl.get_item(Key={"token": token}).get("Item")
        if not item: return False
        if item.get("secret_fp") != admin_secret_fp():   # v9.5: secret rotated (or pre-v9.5 session)
            return False
        now_ts = int(datetime.now(timezone.utc).timestamp())
        return item.get("expires_at", 0) >= now_ts
    except Exception as e:
        print(f"[CW] get_admin_auth token lookup failed: {e}")
        return False

