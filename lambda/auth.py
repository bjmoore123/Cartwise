"""lambda/auth.py -- Authentication: register, login, logout, change-pin, verify-email.

Architecture #7: login() takes explicit event parameter (no __event__ hack).
PDC-03: brute-force lockout after 5 failures.
PDC-18: logout sets valid_after to invalidate all other sessions.
Email verification: accounts start unverified; no alert emails are sent until
the user clicks the link in the verification email sent at registration.
"""
import os
import re
import secrets
import urllib.parse
from html import escape as _esc
from datetime import datetime, timezone, timedelta

from helpers import (
    ok, err, _to_py, hash_pin, verify_pin,
    get_session, users_table, sessions_table,
    DEFAULT_PREFS, SESSION_TTL_HOURS, FRONTEND_URL, API_URL,
    LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES, EMAIL_RE,
    PIN_DENYLIST, check_rate_limit, _get_client_ip,
    SESSION_COOKIE_NAME, make_cookie,
)
from logging_utils import _log_auth_event

VERIFY_TOKEN_TTL_HOURS = 48


def _send_verification_email(email: str, token: str) -> None:
    """Send an email-verification link to the newly registered user."""
    resend_key  = os.environ.get("RESEND_API_KEY", "")
    resend_from = (
        f"{os.environ.get('RESEND_FROM_NAME', 'Cartwise Alerts')} "
        f"<{os.environ.get('RESEND_FROM_ADDR', 'onboarding@resend.dev')}>"
    )
    if not resend_key:
        print("[CW] _send_verification_email: no RESEND_API_KEY -- skipping")
        return

    # v9.5: encode the query; a raw "+" (ben+test@x.com) arrives as a space and fails verification
    verify_url = f"{API_URL}/auth/verify-email?" + urllib.parse.urlencode({"token": token, "email": email})
    app_url    = FRONTEND_URL or "#"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:480px;margin:32px auto;color:#1a1a1a">
  <h2 style="color:#1a6b3c">Verify your Cartwise email</h2>
  <p>Thanks for signing up! Click below to verify <strong>{_esc(email)}</strong>
     and activate weekly deal alerts.</p>
  <p style="margin:24px 0">
    <a href="{_esc(verify_url)}"
       style="background:#1a6b3c;color:#fff;padding:12px 24px;border-radius:8px;
              text-decoration:none;font-weight:600;display:inline-block">
      Verify Email Address
    </a>
  </p>
  <p style="font-size:13px;color:#666">
    This link expires in 48 hours. If you didn't create a Cartwise account,
    you can safely ignore this email.
  </p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
  <p style="font-size:12px;color:#999">
    <a href="{app_url}" style="color:#1a6b3c">cartwise.shopping</a>
  </p>
</body></html>"""

    try:
        import resend as _resend
        _resend.api_key = resend_key
        _resend.Emails.send({
            "from":    resend_from,
            "to":      [email],
            "subject": "Verify your Cartwise email address",
            "html":    html,
        })
        print(f"[CW] Verification email sent -> {email}")
    except Exception as e:
        print(f"[CW] _send_verification_email failed: {e}")


def _verify_html_response(success: bool, message: str, already: bool = False) -> dict:
    """Return an HTML page response for the email verification link click."""
    app_url = FRONTEND_URL or "#"
    icon    = "✅" if success else "❌"
    color   = "#1a6b3c" if success else "#dc2626"
    if success:
        cta = f'<p><a href="{app_url}" style="background:#1a6b3c;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block">Go to Cartwise</a></p>'
    else:
        cta = f'<p><a href="{app_url}" style="color:#1a6b3c">Return to Cartwise</a></p>'

    html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:420px;margin:64px auto;color:#1a1a1a;text-align:center">
  <div style="font-size:48px;margin-bottom:16px">{icon}</div>
  <h2 style="color:{color}">{message}</h2>
  {'<p>You can now receive weekly deal alerts.</p>' if success and not already else ''}
  {cta}
</body></html>"""

    return {
        "statusCode": 200 if success else 400,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def register(body, event=None, _admin=False):
    email = (body.get("email") or "").strip().lower()
    pin   = str(body.get("pin") or "").strip()
    if not email.isascii() or not re.match(EMAIL_RE, email):
        return err("Invalid email address.")
    if not re.match(r'^\d{4}$', pin):
        return err("PIN must be exactly 4 digits.")
    # PDC-21: reject common/weak PINs
    if pin in PIN_DENYLIST:
        return err("That PIN is too common. Please choose a less obvious PIN.")
    # PDC-21: rate-limit registrations per IP (5/hour) — skip for admin
    if event and not _admin:
        ip = _get_client_ip(event)
        if not check_rate_limit("register", ip, 5):
            return err("Too many registration attempts. Please try again later.", 429)
    # PDC-21: return same message whether account exists or not (prevent email enumeration)
    # Admin callers get the explicit duplicate error for clarity.
    _generic_msg = "If this email is not already registered, a verification email has been sent."
    if users_table.get_item(Key={"email": email}).get("Item"):
        if _admin:
            return err("An account with that email already exists.", 409)
        return ok({"message": _generic_msg, "email_verified": False}, 201)
    prefs = {**DEFAULT_PREFS, "notify_email": email}
    verify_token     = secrets.token_urlsafe(32)
    verify_token_exp = (
        datetime.now(timezone.utc) + timedelta(hours=VERIFY_TOKEN_TTL_HOURS)
    ).isoformat()
    users_table.put_item(Item={
        "email":            email,
        "pin_hash":         hash_pin(pin),
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "prefs":            prefs,
        "email_verified":   False,
        "verify_token":     verify_token,
        "verify_token_exp": verify_token_exp,
    })
    _send_verification_email(email, verify_token)
    return ok({
        "message": _generic_msg,
        "email_verified": False,
    }, 201)


def verify_email(body):
    """GET /auth/verify-email -- validate token and mark account as verified.
    Returns an HTML page so clicking the link in the email just works in any browser.
    """
    token = (body.get("token") or "").strip()
    email = (body.get("email") or "").strip().lower()
    if not token or not email:
        return _verify_html_response(False, "Missing token or email.")

    item = users_table.get_item(Key={"email": email}).get("Item")
    if not item:
        return _verify_html_response(False, "Account not found.")

    user = _to_py(item)

    if user.get("email_verified"):
        return _verify_html_response(True, "Email already verified.", already=True)

    stored_token = user.get("verify_token", "")
    token_exp    = user.get("verify_token_exp", "")

    if not stored_token or not secrets.compare_digest(stored_token, token):
        return _verify_html_response(False, "Invalid or expired verification link.")

    if token_exp:
        try:
            exp_dt = datetime.fromisoformat(token_exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return _verify_html_response(
                    False, "Verification link has expired. Please log in and request a new one."
                )
        except Exception:
            pass

    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET email_verified = :t REMOVE verify_token, verify_token_exp",
        ExpressionAttributeValues={":t": True},
    )
    print(f"[CW] Email verified: {email}")
    return _verify_html_response(True, "Email verified successfully!")


def resend_verification(event, body):
    """POST /auth/resend-verification -- resend verification email for unverified accounts.
    PDC-21: rate-limited to 3 per email per hour."""
    email = (body.get("email") or "").strip().lower()
    if not email:
        return err("Email required.")
    # PDC-21: rate-limit by email to prevent email bombing
    if not check_rate_limit("resend-verify", email, 3):
        return ok({"message": "If that account exists, a verification email has been sent."})
    item = users_table.get_item(Key={"email": email}).get("Item")
    if not item:
        return ok({"message": "If that account exists, a verification email has been sent."})
    user = _to_py(item)
    if user.get("email_verified"):
        return ok({"message": "Email is already verified."})
    verify_token     = secrets.token_urlsafe(32)
    verify_token_exp = (
        datetime.now(timezone.utc) + timedelta(hours=VERIFY_TOKEN_TTL_HOURS)
    ).isoformat()
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET verify_token = :t, verify_token_exp = :e",
        ExpressionAttributeValues={":t": verify_token, ":e": verify_token_exp},
    )
    _send_verification_email(email, verify_token)
    return ok({"message": "Verification email sent. Please check your inbox."})


def login(event, body):
    """Architecture #7: takes explicit event instead of body['__event__'] hack."""
    email = (body.get("email") or "").strip().lower()
    pin   = str(body.get("pin") or "").strip()
    now   = datetime.now(timezone.utc)

    user = users_table.get_item(Key={"email": email}).get("Item")

    # Timing-safe: always run verify_pin even when user not found
    if not user:
        verify_pin(pin, "")
        _log_auth_event(event, email, False, "bad_pin")
        return err("Invalid email or PIN.", 401)

    user = _to_py(user)

    # -- Check lockout -------------------------------------------------------
    lockout_until_str = user.get("lockout_until", "")
    if lockout_until_str:
        try:
            lockout_until = datetime.fromisoformat(lockout_until_str)
            if lockout_until.tzinfo is None:
                lockout_until = lockout_until.replace(tzinfo=timezone.utc)
            if now < lockout_until:
                remaining_secs = int((lockout_until - now).total_seconds())
                remaining_mins = max(1, (remaining_secs + 59) // 60)
                _log_auth_event(event, email, False, "locked_out")
                return err(
                    f"Account locked. Try again in {remaining_mins} minute{'s' if remaining_mins != 1 else ''}.",
                    429,
                    _extra={"locked_until": lockout_until.isoformat(),
                            "retry_after_seconds": remaining_secs}
                )
        except Exception as e:
            print(f"[CW] lockout parse error: {e}")

    # -- Verify PIN ----------------------------------------------------------
    pin_ok = verify_pin(pin, user.get("pin_hash", ""))

    if not pin_ok:
        attempts    = int(user.get("failed_attempts", 0)) + 1
        update_expr = "SET failed_attempts = :a, last_failed_at = :t"
        expr_vals   = {":a": attempts, ":t": now.isoformat()}

        if attempts >= LOGIN_MAX_ATTEMPTS:
            lockout_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
            update_expr  += ", lockout_until = :lu"
            expr_vals[":lu"] = lockout_until.isoformat()
            users_table.update_item(
                Key={"email": email},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_vals,
            )
            _log_auth_event(event, email, False, "locked_out")
            return err(
                f"Too many failed attempts. Account locked for {LOGIN_LOCKOUT_MINUTES} minutes.",
                429,
                _extra={"locked_until": lockout_until.isoformat()},
            )

        users_table.update_item(
            Key={"email": email},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_vals,
        )
        remaining = LOGIN_MAX_ATTEMPTS - attempts
        _log_auth_event(event, email, False, "bad_pin")
        # PDC-21: same error message whether account exists or not (prevent enumeration)
        return err("Invalid email or PIN.", 401)

    # -- Success -- clear lockout state -------------------------------------
    # v9.8.3: record last_login for the Admin user table
    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET last_login = :t REMOVE failed_attempts, lockout_until, last_failed_at",
        ExpressionAttributeValues={":t": now.isoformat()},
    )
    token      = secrets.token_hex(32)
    expires_at = int((now + timedelta(hours=SESSION_TTL_HOURS)).timestamp())
    sessions_table.put_item(Item={
        "token":              token,
        "email":              email,
        "expires_at":         expires_at,
        "session_created_at": int(now.timestamp()),
    })
    _log_auth_event(event, email, True)
    prefs = _to_py(user.get("prefs", DEFAULT_PREFS))
    if not prefs.get("notify_email"):
        prefs["notify_email"] = email
    # PDC-05: set HttpOnly cookie alongside the JSON token.
    # PDC-21: SameSite=Lax. Site and API share the parent domain (COOKIE_DOMAIN),
    # so Lax works and gives stronger CSRF protection. Name is per environment.
    cookie = make_cookie(SESSION_COOKIE_NAME, token, SESSION_TTL_HOURS * 3600)
    # Include email_verified in login response so frontend can show banner for unverified accounts.
    # Default True for pre-existing accounts that predate the verification system.
    email_verified = user.get("email_verified", True)
    response = ok({"token": token, "email": email, "prefs": prefs,
                   "email_verified": email_verified}, _event=event)
    response["cookies"] = [cookie]
    return response


def logout(event):
    """PDC-18: delete current session and set valid_after to reject all older tokens."""
    sess = get_session(event)
    if sess:
        sessions_table.delete_item(Key={"token": sess.get("token", "")})
        try:
            users_table.update_item(
                Key={"email": sess["email"]},
                UpdateExpression="SET valid_after = :t",
                ExpressionAttributeValues={":t": int(datetime.now(timezone.utc).timestamp())},
            )
        except Exception as e:
            print(f"[CW] logout valid_after update failed: {e}")
    response = ok({"message": "Logged out."}, _event=event)  # PDC-05: clear cookie
    response["cookies"] = [make_cookie(SESSION_COOKIE_NAME, "", 0)]
    return response


def change_pin(event, body):
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    current_pin = str(body.get("current_pin") or "").strip()
    new_pin     = str(body.get("new_pin") or "").strip()
    if not re.match(r'^\d{4}$', new_pin): return err("New PIN must be 4 digits.")
    # PDC-21: reject common/weak PINs
    if new_pin in PIN_DENYLIST:
        return err("That PIN is too common. Please choose a less obvious PIN.")
    user = users_table.get_item(Key={"email": sess["email"]}).get("Item")
    if not user or not verify_pin(current_pin, user.get("pin_hash", "")):
        return err("Current PIN is incorrect.", 401)
    # PDC-21: invalidate all existing sessions on PIN change (same pattern as logout)
    users_table.update_item(
        Key={"email": sess["email"]},
        UpdateExpression="SET pin_hash = :p, valid_after = :t",
        ExpressionAttributeValues={
            ":p": hash_pin(new_pin),
            ":t": int(datetime.now(timezone.utc).timestamp()),
        },
    )
    return ok({"message": "PIN updated. You will need to log in again."})
