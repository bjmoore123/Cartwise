"""lambda/feedback.py — in-app Contact / Feedback (v9.9).

POST /user/feedback                 signed-in, verified users send a message
GET  /admin/feedback                admin: list messages, newest first
POST /admin/feedback/{id}           admin: set status ("new" | "done")

Safety:
  - Signed-in, verified accounts only (no anonymous form, so no bot spam, and a
    real address to reply to).
  - Rate limits: 5 per hour and 20 per day per account; 2,000-character limit;
    fixed list of kinds.
  - The message is untrusted text: stored as-is, escaped wherever it is shown
    (notification email and Admin). Nothing in it becomes a link or a header.
  - The notification's reply-to is the session's verified account email, never
    anything the user typed. The admin address (FEEDBACK_TO) lives in SSM and
    is never sent to the browser.
  - The message is saved before the email is sent; a failed email never loses it.
"""
import os
import re
import secrets
from datetime import datetime, timezone, timedelta
from html import escape

from helpers import (ok, err, _to_py, get_session, check_rate_limit, users_table, _scan_all, dynamodb)
from logging_utils import _log_app_event

feedback_tbl = dynamodb.Table(os.environ.get("FEEDBACK_TABLE", "cartwise-feedback"))

KINDS        = {"bug": "Bug", "idea": "Idea", "question": "Question", "other": "Other"}
STATUSES     = ("new", "done")
MAX_LEN      = 2000
PER_HOUR     = 5
PER_DAY      = 20
KEEP_DAYS    = 365
PAGES        = {"deals", "list", "schedule", "store", "account", "admin", ""}
_CTRL        = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_message(text: str) -> str:
    """Normalise newlines, drop control characters, trim."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL.sub("", t).strip()


def _one_line(s: str, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def _daily_ok(email: str) -> bool:
    """20 per day: the hourly limiter keyed by the date instead of the hour."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return check_rate_limit("feedback-day", f"{email}#{day}", PER_DAY)


def submit_feedback(event, body):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    email = sess["email"]
    user = _to_py(users_table.get_item(Key={"email": email}).get("Item") or {})
    if not user:
        return err("Not authenticated.", 401)
    if not user.get("email_verified", True):
        return err("Please verify your email address first, so we can reply.", 403)

    kind = (body.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return err("Please choose a type.")
    message = _clean_message(body.get("message"))
    if not message:
        return err("Please enter a message.")
    if len(message) > MAX_LEN:
        return err(f"Please keep it under {MAX_LEN:,} characters.")

    if not check_rate_limit("feedback", email, PER_HOUR) or not _daily_ok(email):
        return err("Thanks! You've sent several messages recently. Please try again later.", 429)

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    prefs = user.get("prefs") or {}
    page = _one_line(body.get("page"), 20).lower()
    now = datetime.now(timezone.utc)
    item = {
        "feedback_id": f"{now.isoformat()}#{secrets.token_hex(4)}",
        "ts":          now.isoformat(),
        "email":       email,
        "kind":        kind,
        "message":     message,
        "status":      "new",
        "page":        page if page in PAGES else "",
        "app_version": _one_line(body.get("app_version"), 20),
        "store_id":    _one_line(prefs.get("store_id"), 10),
        "store_name":  _one_line(prefs.get("store_name"), 80),
        "user_agent":  _one_line(headers.get("user-agent"), 200),
        "expires_at":  int((now + timedelta(days=KEEP_DAYS)).timestamp()),
    }
    item = {k: v for k, v in item.items() if v != ""}
    try:
        feedback_tbl.put_item(Item=item)
    except Exception as e:
        print(f"[CW] feedback save failed: {e}")
        return err("Sorry, we couldn't send that. Please try again.", 500)

    emailed = _notify(item)
    _log_app_event("api", "info", action="feedback-received", user=email, kind=kind,
                   emailed=emailed, feedback_id=item["feedback_id"])
    return ok({"message": "Thanks, we got it. We'll reply to your sign-in email if needed."})


def _notify(item: dict) -> bool:
    """Email the admin. Never raises; returns whether it was sent."""
    to = os.environ.get("FEEDBACK_TO", "").strip()
    key = os.environ.get("RESEND_API_KEY", "")
    if not to or not key:
        print("[CW] feedback: FEEDBACK_TO or RESEND_API_KEY not set; saved, not emailed")
        return False
    kind = KINDS.get(item["kind"], "Other")
    email = item["email"]
    first = _one_line(item["message"], 60)
    env = " staging" if "staging" in os.environ.get("FEEDBACK_TABLE", "") else ""   # staging shares the SSM address
    subject = f"[Cartwise{env} feedback] {kind} from {email}: {first}"   # sent as JSON, no header injection
    ctx = [("From", email), ("Type", kind), ("Page", item.get("page", "")),
           ("Store", f'#{item["store_id"]} {item.get("store_name", "")}'.strip() if item.get("store_id") else ""),
           ("App", item.get("app_version", "")), ("Browser", item.get("user_agent", "")), ("Received", item["ts"])]
    rows = "".join(f'<tr><td style="padding:2px 12px 2px 0;color:#777;white-space:nowrap">{escape(k)}</td>'
                   f'<td style="padding:2px 0">{escape(v)}</td></tr>' for k, v in ctx if v)
    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:640px;margin:24px auto;color:#222">
<div style="background:#1a6b3c;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
  <div style="font-size:18px;font-weight:bold">Cartwise feedback · {escape(kind)}</div></div>
<div style="border:1px solid #ddd;border-top:none;padding:18px 20px;border-radius:0 0 8px 8px">
  <div style="white-space:pre-wrap;font-size:15px;line-height:1.5;border-left:3px solid #1a6b3c;padding-left:12px">{escape(item["message"])}</div>
  <table style="margin-top:16px;font-size:12px">{rows}</table>
  <p style="font-size:12px;color:#777;margin-top:14px">Reply to this email to answer {escape(email)}. Also in Admin → Feedback.</p>
</div></body></html>"""
    text = item["message"] + "\n\n" + "\n".join(f"{k}: {v}" for k, v in ctx if v)
    sender = (f"{os.environ.get('RESEND_FROM_NAME', 'Cartwise Alerts')} "
              f"<{os.environ.get('RESEND_FROM_ADDR', 'onboarding@resend.dev')}>")
    try:
        import resend as _resend
        _resend.api_key = key
        _resend.Emails.send({"from": sender, "to": [to], "reply_to": [email],
                             "subject": subject, "html": html, "text": text})
        return True
    except Exception as e:
        print(f"[CW] feedback email failed: {e}")
        _log_app_event("email", "error", ok=False, to="(admin)", subject="feedback", message=str(e)[:200])
        return False


# ── Admin ────────────────────────────────────────────────────────────────────

def admin_list_feedback(event):
    """Every message, newest first (a year of feedback is small)."""
    try:
        items = _to_py(_scan_all(feedback_tbl))
        items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        for it in items:
            it.pop("expires_at", None)
        return ok({"feedback": items, "new": sum(1 for i in items if i.get("status") == "new")})
    except Exception as e:
        print(f"[CW] admin_list_feedback: {e}")
        return err("Could not load feedback.", 500)


def admin_update_feedback(event, body, feedback_id: str):
    status = (body.get("status") or "").strip().lower()
    if status not in STATUSES:
        return err("Status must be new or done.")
    try:
        feedback_tbl.update_item(
            Key={"feedback_id": feedback_id},
            UpdateExpression="SET #s = :s",
            ConditionExpression="attribute_exists(feedback_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
        return ok({"feedback_id": feedback_id, "status": status})
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            return err("Not found.", 404)
        print(f"[CW] admin_update_feedback: {e}")
        return err("Could not update.", 500)
