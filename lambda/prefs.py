"""lambda/prefs.py — User preferences, account deletion, unsubscribe, test email.

PDC-09: save_prefs filters to ALLOWED_PREF_KEYS; validates threshold and item lengths.
"""
import os
import re
import urllib.parse

from helpers import (
    ok, err, _to_py, get_session,
    users_table, sessions_table, revoke_sessions, count_deleted_user,
    DEFAULT_PREFS, ALLOWED_PREF_KEYS,
    FRONTEND_URL, API_URL,
    UNSUB_SECRET, _verify_unsub_token,
    EMAIL_RE, check_rate_limit,
)
from logging_utils import _log_app_event


# ── Prefs ─────────────────────────────────────────────────────────────────────

def get_prefs(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    if not prefs.get("notify_email"): prefs["notify_email"] = sess["email"]
    # v9.9: lets a restored session (page reload) know whether to show "Verify your email"
    return ok({"prefs": prefs, "email": sess["email"],
               "email_verified": _to_py(user).get("email_verified", True) is not False})


def save_prefs(event, body):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    incoming = body.get("prefs", body)
    # PDC-09: filter to allowed keys only — reject arbitrary key injection
    filtered = {k: v for k, v in incoming.items() if k in ALLOWED_PREF_KEYS}
    prefs    = {**DEFAULT_PREFS, **filtered}
    # Validate notify_email
    ne = (prefs.get("notify_email") or "").strip()
    if ne and (not ne.isascii() or not re.match(EMAIL_RE, ne)):
        return err("Invalid notification email address.")
    # PDC-21: restrict notify_email to login email to prevent email relay abuse
    if ne and ne.lower() != sess["email"].lower():
        return err("Notification email must match your login email.")
    if not ne: prefs["notify_email"] = sess["email"]
    # PDC-09: validate matching config — accept either threshold (int 40-100) or sensitivity string
    matching = prefs.get("matching") or {}
    raw_thresh = matching.get("threshold")
    if isinstance(raw_thresh, (int, float)) and 40 <= int(raw_thresh) <= 100:
        matching = {"threshold": int(raw_thresh)}
    else:
        sens = matching.get("sensitivity", "normal")
        if sens not in ("strict", "normal", "loose"):
            sens = "normal"
        matching = {"sensitivity": sens}
    prefs["matching"] = matching
    # v9.8: BOGO rule — "auto" follows the store's state; "half" / "two" are the user's choice
    if prefs.get("bogo_mode") not in ("auto", "half", "two"):
        prefs["bogo_mode"] = "auto"
    # PDC-09: cap item strings at 200 chars each, cap list at 200 items
    if isinstance(prefs.get("items"), list):
        prefs["items"] = [
            (str(i.get("name", ""))[:200] if isinstance(i, dict) else str(i)[:200])
            for i in prefs["items"][:200]
            if (i.get("name") if isinstance(i, dict) else i)
        ]
    users_table.update_item(
        Key={"email": sess["email"]},
        UpdateExpression="SET prefs = :p",
        ExpressionAttributeValues={":p": prefs},
    )
    return ok({"message": "Preferences saved.", "prefs": prefs})


def delete_account(event):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    users_table.delete_item(Key={"email": sess["email"]})
    sessions_table.delete_item(Key={"token": sess.get("token", "")})
    revoke_sessions(sess["email"])              # v9.5: every other device too
    count_deleted_user()
    return ok({"message": "Account deleted."})


# ── Unsubscribe ───────────────────────────────────────────────────────────────

def unsubscribe(event):
    """
    GET  /user/unsubscribe?email=...&token=...  → HTML confirmation page
    POST /user/unsubscribe?email=...&token=...  → sets email_enabled=False
    Token-only auth; no session required. Scope limited to email_enabled toggle.
    """
    qs     = event.get("queryStringParameters") or {}
    email  = (qs.get("email") or "").strip().lower()
    token  = (qs.get("token") or "").strip()
    method = event.get("httpMethod", event.get("requestContext", {}).get("http", {}).get("method", "GET")).upper()

    if not email or not token:
        return _unsub_html("Invalid Link", "This unsubscribe link is missing required parameters.", success=False)
    if not UNSUB_SECRET or UNSUB_SECRET == "changeme-set-in-deploy":
        return _unsub_html("Configuration Error", "Unsubscribe is not configured on this server.", success=False)
    if not _verify_unsub_token(email, token):
        return _unsub_html("Invalid Token", "This unsubscribe link is not valid or has expired.", success=False)

    item = users_table.get_item(Key={"email": email}).get("Item")
    if not item:
        return _unsub_html("Account Not Found", "No account found for this email address.", success=False)

    if method == "GET":
        already_off = not _to_py(item.get("prefs", {})).get("email_enabled", True)
        if already_off:
            return _unsub_html("Already Unsubscribed",
                f"Email alerts are already turned off for <strong>{email}</strong>.")
        return _unsub_confirm_page(email, token)

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

    return _unsub_html("Method Not Allowed", "Unexpected request method.", success=False)


def _unsub_html(title: str, body_html: str, success: bool = True) -> dict:
    colour  = "#1a6b3c" if success else "#c0392b"
    icon    = "✅" if success else "❌"
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
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


def _unsub_confirm_page(email: str, token: str) -> dict:
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
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


# ── Test email ────────────────────────────────────────────────────────────────

def send_test_email(event):
    """POST /user/test-email — sends a test alert to the user's notify_email.
    PDC-21: rate-limited to 1 per user per 10 minutes (6/hour bucket)."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    # PDC-21: rate-limit test emails per user
    if not check_rate_limit("test-email", sess["email"], 6):
        return err("Please wait a few minutes before sending another test email.", 429)
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)
    if not _to_py(user).get("email_verified", True):
        return err("Please verify your email address before sending test emails.", 403)
    prefs        = _to_py(user.get("prefs", DEFAULT_PREFS))
    notify_email = (prefs.get("notify_email") or sess["email"]).strip()

    resend_key  = os.environ.get("RESEND_API_KEY", "")
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


def resend_weekly_email(event):
    """POST /user/resend-weekly — re-trigger the scraper for just this user's email.
    PDC-21: rate-limited to 3 per user per hour."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    # PDC-21: rate-limit resend-weekly per user
    if not check_rate_limit("resend-weekly", sess["email"], 3):
        return err("Please wait before requesting another weekly email.", 429)
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user: return err("User not found.", 404)

    user_py = _to_py(user)
    if not user_py.get("email_verified", True):
        return err("Please verify your email address first.", 403)
    prefs     = user_py.get("prefs", DEFAULT_PREFS)
    store_id  = prefs.get("store_id", "")
    if not store_id:
        return err("No store selected. Please choose a store first.")
    if not prefs.get("email_enabled", True):
        return err("Email alerts are disabled. Enable them in Notifications first.")

    notify_email = (prefs.get("notify_email") or sess["email"]).strip()
    scraper_fn   = os.environ.get("SCRAPER_FUNCTION", "")
    if not scraper_fn:
        return err("Scraper function not configured.", 500)

    import json as _json
    try:
        from helpers import lambda_client
        payload = _json.dumps({
            "source":       "manual-resend",
            "send_emails":  True,
            "target_email": sess["email"],
        }).encode()
        lambda_client.invoke(
            FunctionName  = scraper_fn,
            InvocationType= "Event",   # async — don't wait
            Payload       = payload,
        )
        _log_app_event("email", "info", action="resend-weekly-triggered",
                       user=sess["email"], notify_email=notify_email, store_id=store_id)
        return ok({"message": f"Weekly deals email queued for {notify_email}. It will arrive within a few minutes."})
    except Exception as e:
        print(f"[CW] resend_weekly_email invoke: {e}")
        return err("Failed to trigger email.", 500)
