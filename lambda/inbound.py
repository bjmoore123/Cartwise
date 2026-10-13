"""lambda/inbound.py — Resend inbound email webhook for Publix My List import.

Receives a POST from Resend when a user forwards their Publix shopping list email.
Verifies Svix HMAC signature, parses the HTML table, and merges items into the
user's prefs.items list without duplicates.

Spam defences (in order):
  1. Svix HMAC signature — proves Resend sent the webhook.
  2. To-address check — must be addressed to INBOUND_EMAIL_ADDR.
  3. Subject allowlist — subject must contain a Publix/list keyword.
  4. Per-sender rate limit — max 5 emails per sender per hour (DynamoDB TTL).
  5. User lookup — silently drops emails from unregistered senders.
"""
import os
import re
import json
import hmac
import hashlib
import urllib.request
import urllib.error
import html.parser as _hp
from datetime import datetime, timezone, timedelta

from helpers import (
    ok, err,
    _to_py, get_body, itemName,
    users_table,
    DEFAULT_PREFS, CORS,
    RESEND_WEBHOOK_SECRET, INBOUND_EMAIL_ADDR,
)
from logging_utils import _log_app_event
import boto3

# ── Spam filter config ────────────────────────────────────────────────────────

# Subject must contain at least one of these substrings (case-insensitive).
# Publix My List emails always have "My List" or "Publix" in the subject.
SUBJECT_KEYWORDS = ("my list", "publix", "shopping list", "grocery list")

# Max inbound emails accepted from a single sender address per hour.
RATE_LIMIT_PER_HOUR = 5

_rate_tbl_name = os.environ.get("APP_LOGS_TABLE", "cartwise-app-logs")
# Re-use app-logs table with a synthetic log_id key — avoids needing a new table.
# Key format: "inbound-rate#{sender}" with TTL = top of next hour + 1h.
_dynamo = boto3.resource("dynamodb", region_name=os.environ.get("PDC_REGION", "us-east-1"))
_rate_tbl = _dynamo.Table(_rate_tbl_name)


def _check_rate_limit(sender: str) -> bool:
    """Return True if sender is within rate limit, False if they should be blocked.
    Uses an atomic ADD counter in DynamoDB with a 2-hour TTL.
    """
    try:
        now   = datetime.now(timezone.utc)
        # Key resets on the hour — everyone starts fresh each hour
        hour_key = now.strftime("%Y%m%d-%H")
        ddb_key  = f"inbound-rate#{sender}#{hour_key}"
        expires  = int((now.replace(minute=0, second=0, microsecond=0)
                        + timedelta(hours=2)).timestamp())
        resp = _rate_tbl.update_item(
            Key={"log_id": ddb_key},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :exp), "
                             "#src = if_not_exists(#src, :src)",
            ExpressionAttributeNames={"#c": "count", "#ttl": "expires_at",
                                      "#src": "source"},
            ExpressionAttributeValues={":one": 1, ":exp": expires, ":src": "inbound-rate"},
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"].get("count", 1))
        return count <= RATE_LIMIT_PER_HOUR
    except Exception as e:
        # On any DynamoDB error, allow through rather than block legitimate users
        _log_app_event("api", "warn", action="inbound-rate-limit-error", message=str(e)[:200])
        return True


# ── Svix signature verification ───────────────────────────────────────────────

def _verify_resend_webhook(event: dict) -> bool:
    """Verify Svix HMAC signature on Resend webhook requests.
    PDC-21: also validates svix-timestamp within ±5 min to prevent replay attacks."""
    import base64 as _b64
    import time as _time
    secret = RESEND_WEBHOOK_SECRET
    if not secret or secret == "not-yet-configured":
        return False
    if secret.startswith("whsec_"):
        secret_bytes = _b64.b64decode(secret[len("whsec_"):])
    else:
        secret_bytes = secret.encode()
    headers        = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    svix_id        = headers.get("svix-id", "")
    svix_timestamp = headers.get("svix-timestamp", "")
    svix_signature = headers.get("svix-signature", "")
    # PDC-21: reject replayed webhooks — timestamp must be within ±5 minutes
    try:
        ts = int(svix_timestamp)
        if abs(_time.time() - ts) > 300:
            return False
    except (ValueError, TypeError):
        return False
    raw_body       = event.get("body", "")
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


# ── Publix My List HTML parser ────────────────────────────────────────────────

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


def _extract_email(addr_str: str) -> str:
    """Extract bare email from 'Name <email>' or plain email string."""
    if not addr_str:
        return ""
    m = re.search(r"<([^>]+)>", addr_str)
    if m:
        return m.group(1).strip().lower()
    return addr_str.strip().lower()


# ── Main webhook handler ──────────────────────────────────────────────────────

def inbound_email_list(event):
    """POST /inbound/email-list — Resend inbound webhook for Publix list import."""
    sig_ok = _verify_resend_webhook(event)
    if not sig_ok:
        secret_set = bool(RESEND_WEBHOOK_SECRET and RESEND_WEBHOOK_SECRET != "not-yet-configured")
        _log_app_event("api", "warn", action="inbound-sig-fail",
                       secret_configured=secret_set,
                       has_svix_id=bool((event.get("headers") or {}).get("svix-id")),
                       message="Webhook signature verification failed — check RESEND_WEBHOOK_SECRET")
        return {"statusCode": 400, "headers": CORS, "body": json.dumps({"error": "Bad signature"})}

    body     = get_body(event)
    evt_type = body.get("type", "")
    _log_app_event("api", "info", action="inbound-webhook-received",
                   event_type=evt_type, has_data=bool(body.get("data")))

    if evt_type != "email.received":
        return ok({"skipped": True, "reason": "event type not email.received"})

    data     = body.get("data", {})
    email_id = data.get("email_id", "")
    to_addrs = data.get("to", [])
    raw_from = data.get("from", "")

    if INBOUND_EMAIL_ADDR:
        match = any(INBOUND_EMAIL_ADDR.lower() in a.lower() for a in to_addrs)
        if not match:
            _log_app_event("api", "warn", action="inbound-wrong-dest",
                           to_addrs=str(to_addrs)[:200], expected=INBOUND_EMAIL_ADDR,
                           message="Inbound email arrived at unexpected destination address")
            return ok({"skipped": True, "reason": "not our inbound address"})

    sender_email = _extract_email(raw_from)
    _log_app_event("api", "info", action="inbound-sender-extracted",
                   email_id=email_id, sender=sender_email,
                   raw_from=raw_from[:100], to_addrs=str(to_addrs)[:200])
    if not sender_email:
        _log_app_event("api", "warn", action="inbound-no-from",
                       raw_from=raw_from[:100],
                       message="Could not extract sender email from from: header")
        return ok({"skipped": True, "reason": "no from address"})

    # ── Spam filter 1: subject allowlist ─────────────────────────────────────
    subject = (data.get("subject") or "").lower()
    if not any(kw in subject for kw in SUBJECT_KEYWORDS):
        _log_app_event("api", "warn", action="inbound-subject-rejected",
                       sender=sender_email, email_id=email_id, subject=subject[:120],
                       message="Inbound email rejected — subject did not match allowlist")
        return ok({"skipped": True, "reason": "subject not recognized"})

    # ── Spam filter 2: per-sender rate limit ──────────────────────────────────
    if not _check_rate_limit(sender_email):
        _log_app_event("api", "warn", action="inbound-rate-limited",
                       sender=sender_email, email_id=email_id,
                       message=f"Inbound rate limit exceeded ({RATE_LIMIT_PER_HOUR}/hr)")
        return ok({"skipped": True, "reason": "rate limit exceeded"})

    # Match user — login email first, then notify_email
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

    # No fallback scan needed: PDC-21 locked notify_email to match login email,
    # so get_item by sender_email is always sufficient. The old scan on
    # prefs.notify_email was dead code that would never match.

    if not user:
        # v9.7: log it (sender only) so Admin → Inbound shows why nothing was imported
        _log_app_event("api", "info", action="inbound-no-user", sender=sender_email, email_id=email_id,
                       message="No Cartwise account for this sender; email ignored")
        return ok({"skipped": True})

    # v9.7: reading a received email needs a Resend key that can read, not just send.
    # Resend's "sending access" keys "can only send emails", which is why this call
    # returned 403 since v9.1. Use RESEND_RECEIVING_KEY (a full-access key, from SSM
    # /publix/resend-receiving-key) when set; fall back to the sending key.
    resend_key = os.environ.get("RESEND_RECEIVING_KEY", "") or os.environ.get("RESEND_API_KEY", "")
    key_kind   = "receiving" if os.environ.get("RESEND_RECEIVING_KEY") else "sending"
    if not resend_key or not email_id:
        _log_app_event("api", "error", action="inbound-config-error",
                       has_key=bool(resend_key), has_email_id=bool(email_id))
        return {"statusCode": 500, "headers": CORS, "body": json.dumps({"error": "Configuration error"})}

    _log_app_event("api", "info", action="inbound-fetching-body",
                   email_id=email_id, user=user["email"])
    try:
        req = urllib.request.Request(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {resend_key}", "Accept": "application/json", "User-Agent": "cartwise/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            email_data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        hint = ("Resend refused to let this key read received email. Create a Full access API key in "
                "Resend, store it in SSM /publix/resend-receiving-key, and redeploy."
                if e.code in (401, 403) and key_kind == "sending" else
                "Check the receiving key is valid and the email_id exists in Resend")
        _log_app_event("api", "error", action="inbound-fetch-failed",
                       email_id=email_id, user=user["email"],
                       message=f"HTTP Error {e.code}: {e.reason}",
                       resend_error=error_body, key_kind=key_kind,
                       has_key=bool(resend_key), hint=hint)
        return {"statusCode": 502, "headers": CORS,
                "body": json.dumps({"error": "Failed to fetch email content"})}
    except Exception as e:
        _log_app_event("api", "error", action="inbound-fetch-failed",
                       email_id=email_id, user=user["email"], message=str(e)[:200],
                       hint="Check RESEND_API_KEY is valid and email_id exists in Resend")
        return {"statusCode": 502, "headers": CORS,
                "body": json.dumps({"error": "Failed to fetch email content"})}

    html_body    = email_data.get("html") or ""
    html_len     = len(html_body)
    parsed_names = _parse_publix_list_html(html_body)
    _log_app_event("api", "info", action="inbound-html-parsed",
                   user=user["email"], email_id=email_id, html_bytes=html_len,
                   items_found=len(parsed_names), items_preview=str(parsed_names[:5]),
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

    prefs    = _to_py(user.get("prefs", DEFAULT_PREFS))
    existing = {itemName(i).lower() for i in (prefs.get("items") or [])}
    added, added_names, skipped_names = 0, [], []
    for name in parsed_names:
        if name.lower() not in existing:
            prefs.setdefault("items", []).append(name)
            existing.add(name.lower())
            added += 1
            added_names.append(name)
        else:
            skipped_names.append(name)

    users_table.update_item(
        Key={"email": user["email"]},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs},
    )
    _log_app_event("api", "info", action="inbound-list-imported",
                   user=user["email"], added=added, total_parsed=len(parsed_names),
                   skipped_duplicates=len(skipped_names),
                   added_items=str(added_names[:20]),   # v9.7: the ones actually added
                   skipped_items=str(skipped_names[:10]))
    return ok({"imported": added, "total_parsed": len(parsed_names)})
