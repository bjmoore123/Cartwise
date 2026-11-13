"""lambda/auth.py -- Authentication: register, login, logout, change-pin, verify-email.

Architecture #7: login() takes explicit event parameter (no __event__ hack).
PDC-03: brute-force lockout after 5 failures.
PDC-18: logout sets valid_after to invalidate all other sessions.
Email verification: accounts start unverified; no alert emails are sent until
the user clicks the link in the verification email sent at registration.
"""
import hashlib
import os
import re
import secrets
import time
import urllib.parse
from html import escape as _esc
from datetime import datetime, timezone, timedelta

from helpers import (
    ok, err, _to_py, hash_pin, verify_pin,
    get_session, users_table, sessions_table,
    DEFAULT_PREFS, SESSION_TTL_HOURS, FRONTEND_URL, API_URL,
    LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_MINUTES, EMAIL_RE,
    PIN_DENYLIST, check_rate_limit, _get_client_ip,
    SESSION_COOKIE_NAME, make_cookie, revoke_sessions,
)
from logging_utils import _log_auth_event
from shared import unsub as _unsub
from shared import email_layout as L

VERIFY_TOKEN_TTL_HOURS = 48


def _unsub_link(email: str) -> str:
    import helpers
    return _unsub.url(API_URL, helpers.UNSUB_SECRET, email)


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
    link   = _unsub_link(email)
    hdrs   = _unsub.headers(link)
    reason = "Sent because this address signed up for Cartwise."
    html = L.email(
        title="Verify your Cartwise email", preheader="One click to turn on your weekly Publix deal alerts.",
        heading="Confirm your email",
        body=(L.p(f"Thanks for signing up! Confirm {L.addr(email)} so we can start sending your weekly deal alerts.")
              + L.button("Verify my email", verify_url)
              + L.p("This link expires in 48 hours. If you didn't create a Cartwise account, you can ignore "
                    "this email.", small=True, muted=True, last=True)),
        unsub_url=link, reason=reason, site=FRONTEND_URL)
    text = L.text("Confirm your Cartwise email",
                  f"Thanks for signing up! Confirm {email} so we can start sending your weekly deal alerts:\n{verify_url}",
                  "This link expires in 48 hours. If you didn't create a Cartwise account, you can ignore this email.",
                  unsub_url=link, reason=reason, site=FRONTEND_URL)

    try:
        import resend as _resend
        _resend.api_key = resend_key
        msg = {
            "from":    resend_from,
            "to":      [email],
            "subject": "Verify your Cartwise email address",
            "html":    html,
            "text":    text,
        }
        if hdrs:
            msg["headers"] = hdrs
        _resend.Emails.send(msg)
        print(f"[CW] Verification email sent -> {email}")
    except Exception as e:
        print(f"[CW] _send_verification_email failed: {e}")


def _verify_html_response(success: bool, message: str, already: bool = False) -> dict:
    """The branded page shown after a verification link is clicked."""
    app_url = _esc((FRONTEND_URL or "https://www.cartwise.shopping").rstrip("/"))
    if success:
        detail = ("Your weekly deal alerts are on." if not already else "Nothing else to do.")
        action = f'<a class="btn" href="{app_url}">Go to Cartwise</a>'
    else:
        detail = "Request a new link from the banner after you sign in."
        action = f'<a class="btn" href="{app_url}">Sign in to Cartwise</a>'
    return L.landing_page(message.rstrip("!"), detail, ok=success, action_html=action,
                          site=FRONTEND_URL, status=200 if success else 400)


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
        return _verify_html_response(False, "This link isn't valid")

    item = users_table.get_item(Key={"email": email}).get("Item")
    if not item:
        return _verify_html_response(False, "This link isn't valid")

    user = _to_py(item)

    if user.get("email_verified"):
        return _verify_html_response(True, "Your email is already verified", already=True)

    stored_token = user.get("verify_token", "")
    token_exp    = user.get("verify_token_exp", "")

    if not stored_token or not secrets.compare_digest(stored_token, token):
        return _verify_html_response(False, "This link isn't valid")

    if token_exp:
        try:
            exp_dt = datetime.fromisoformat(token_exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                return _verify_html_response(
                    False, "This link has expired"
                )
        except Exception:
            pass

    users_table.update_item(
        Key={"email": email},
        UpdateExpression="SET email_verified = :t REMOVE verify_token, verify_token_exp",
        ExpressionAttributeValues={":t": True},
    )
    print(f"[CW] Email verified: {email}")
    return _verify_html_response(True, "Your email is verified")


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


# ── Forgot PIN (v9.11) ────────────────────────────────────────────────────────
# Self-service PIN reset from the sign-in page. Safety rules, same as sign-up and
# email verification:
#   - The request never says whether an account exists: one message for every
#     outcome (no account, rate-limited, sent), and a fixed minimum response time
#     so "we sent an email" can't be told apart by how long it took.
#   - Rate limits: 3 an hour per email, 10 an hour per IP (counted whether or not
#     the account exists).
#   - The link holds a random 256-bit token; only its SHA-256 is stored. It lasts
#     30 minutes, works once, and a newer request replaces it. It travels in the
#     URL fragment (#reset?...), which browsers never send to servers or logs.
#   - A bad, used or expired link gets one generic message.
#   - Success signs out every session, clears the lockout, and emails a notice.

PIN_RESET_TTL_MINUTES   = 30
PIN_RESET_MIN_SECONDS   = 1.5      # every /auth/forgot-pin answer takes at least this long
PIN_RESET_REQUEST_MSG   = ("If an account exists for that email, we've sent a link to reset your PIN. "
                           "It expires in 30 minutes.")
PIN_RESET_BAD_LINK_MSG  = "This reset link is invalid or has expired. Please request a new one."


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _send_email(to: str, subject: str, html: str, text: str, headers: dict | None = None) -> bool:
    key = os.environ.get("RESEND_API_KEY", "")
    if not key:
        print("[CW] auth email: no RESEND_API_KEY -- skipping")
        return False
    sender = (f"{os.environ.get('RESEND_FROM_NAME', 'Cartwise Alerts')} "
              f"<{os.environ.get('RESEND_FROM_ADDR', 'onboarding@resend.dev')}>")
    try:
        import resend as _resend
        _resend.api_key = key
        msg = {"from": sender, "to": [to], "subject": subject, "html": html, "text": text}
        if headers:
            msg["headers"] = headers
        _resend.Emails.send(msg)
        return True
    except Exception as e:
        print(f"[CW] auth email failed: {e}")
        return False


def _send_pin_reset_email(email: str, token: str) -> None:
    base = (FRONTEND_URL or "https://cartwise.shopping").rstrip("/")
    link = f"{base}/#reset?" + urllib.parse.urlencode({"token": token, "email": email})
    unsub = _unsub_link(email)
    reason = "Sent because a PIN reset was requested."
    html = L.email(
        title="Reset your Cartwise PIN", preheader=f"This link works once and expires in {PIN_RESET_TTL_MINUTES} minutes.",
        heading="Reset your PIN",
        body=(L.p(f"We got a request to reset the PIN for {L.addr(email)}. If that was you, choose a new one:")
              + L.button("Choose a new PIN", link)
              + L.callout(f"This link works once and expires in {PIN_RESET_TTL_MINUTES} minutes.")
              + L.p("Didn't ask for this? Ignore this email and your PIN stays the same.",
                    small=True, muted=True, last=True)),
        unsub_url=unsub, reason=reason, site=FRONTEND_URL)
    text = L.text("Reset your Cartwise PIN",
                  f"We got a request to reset the PIN for {email}. If that was you, choose a new one here:\n{link}",
                  f"This link works once and expires in {PIN_RESET_TTL_MINUTES} minutes.",
                  "Didn't ask for this? Ignore this email and your PIN stays the same.",
                  unsub_url=unsub, reason=reason, site=FRONTEND_URL)
    _send_email(email, "Reset your Cartwise PIN", html, text, _unsub.headers(unsub))


def _send_pin_changed_email(email: str) -> None:
    base = (FRONTEND_URL or "https://cartwise.shopping").rstrip("/")
    unsub = _unsub_link(email)
    reason = "Security notice for your Cartwise account."
    html = L.email(
        title="Your Cartwise PIN was changed", preheader="Every device was signed out. Wasn't you? Here's what to do.",
        heading="Your PIN was changed",
        body=(L.p(f"The PIN for {L.addr(email)} was just reset using a link sent to this address. "
                  "For your security, every device was signed out.")
              + L.callout("<strong>Wasn't you?</strong> Reset it again with <em>Forgot PIN?</em> on the sign-in "
                          "page, and check who can get into this mailbox.")
              + L.button("Sign in to Cartwise", base)),
        unsub_url=unsub, reason=reason, site=FRONTEND_URL)
    text = L.text("Your Cartwise PIN was changed",
                  f"The PIN for {email} was just reset using a link sent to this address. For your security, "
                  "every device was signed out.",
                  f"Wasn't you? Reset it again with \"Forgot PIN?\" on the sign-in page ({base}) and check who "
                  "can get into this mailbox.",
                  unsub_url=unsub, reason=reason, site=FRONTEND_URL)
    _send_email(email, "Your Cartwise PIN was changed", html, text, _unsub.headers(unsub))


def forgot_pin(event, body):
    """POST /auth/forgot-pin {email} -- email a one-time PIN reset link.
    Always the same answer, in about the same time, whatever happened."""
    started = time.monotonic()
    email = (body.get("email") or "").strip().lower()
    if not email.isascii() or not re.match(EMAIL_RE, email):
        return err("Enter a valid email address.")            # about the input, not about any account

    def done():
        wait = PIN_RESET_MIN_SECONDS - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
        return ok({"message": PIN_RESET_REQUEST_MSG})

    try:
        allowed = check_rate_limit("forgot-pin-ip", _get_client_ip(event), 10)
        allowed = check_rate_limit("forgot-pin", email, 3) and allowed      # count both, always
        if not allowed:
            return done()
        if not users_table.get_item(Key={"email": email}, ProjectionExpression="email").get("Item"):
            return done()
        token = secrets.token_urlsafe(32)
        expires = int(datetime.now(timezone.utc).timestamp()) + PIN_RESET_TTL_MINUTES * 60
        users_table.update_item(
            Key={"email": email},
            UpdateExpression="SET pin_reset_hash = :h, pin_reset_exp = :e",
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeValues={":h": _token_hash(token), ":e": expires},
        )
        _send_pin_reset_email(email, token)
        _log_auth_event(event, email, True, "pin_reset_requested")
    except Exception as e:
        print(f"[CW] forgot_pin: {e}")
    return done()


def _pin_reset_link(email: str, token: str):
    """The account row and stored hash if (email, token) is a live reset link, else None.
    Timing-safe: always compares, even with no account or no pending reset."""
    item = users_table.get_item(Key={"email": email}).get("Item") if email else None
    user = _to_py(item) if item else {}
    stored = str(user.get("pin_reset_hash") or "")
    matches = secrets.compare_digest(stored or _token_hash("no-reset-pending"), _token_hash(token))
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if user and stored and token and matches and int(user.get("pin_reset_exp") or 0) >= now_ts:
        return user, stored
    return None


def check_pin_reset(event, body):
    """POST /auth/check-reset {email, token} -- is this reset link still good?
    Lets the page say "expired" before the user types a new PIN. Only someone holding a
    live link learns anything; every bad link gets the same 400 as reset_pin. Uses nothing up."""
    email = (body.get("email") or "").strip().lower()
    token = str(body.get("token") or "").strip()
    if not check_rate_limit("check-reset-ip", _get_client_ip(event), 30):
        return err("Too many attempts. Please try again later.", 429)
    found = _pin_reset_link(email, token)
    if not found:
        _log_auth_event(event, email, False, "pin_reset_link_check_failed")
        return err(PIN_RESET_BAD_LINK_MSG, 400)
    left = int(found[0].get("pin_reset_exp") or 0) - int(datetime.now(timezone.utc).timestamp())
    return ok({"valid": True, "expires_in": max(0, left)})


def reset_pin(event, body):
    """POST /auth/reset-pin {email, token, new_pin} -- set a new PIN with a reset link."""
    email   = (body.get("email") or "").strip().lower()
    token   = str(body.get("token") or "").strip()
    new_pin = str(body.get("new_pin") or "").strip()
    if not re.match(r'^\d{4}$', new_pin):
        return err("New PIN must be 4 digits.")
    if new_pin in PIN_DENYLIST:
        return err("That PIN is too common. Please choose a less obvious PIN.")
    if not check_rate_limit("reset-pin-ip", _get_client_ip(event), 10) or \
       (email and not check_rate_limit("reset-pin", email, 5)):
        return err("Too many attempts. Please try again later.", 429)

    found = _pin_reset_link(email, token)
    if not found:
        _log_auth_event(event, email, False, "pin_reset_bad_link")
        return err(PIN_RESET_BAD_LINK_MSG, 400)
    stored = found[1]
    now_ts = int(datetime.now(timezone.utc).timestamp())

    try:
        users_table.update_item(
            Key={"email": email},
            # Using the link proves control of the mailbox, so the email counts as verified.
            UpdateExpression="SET pin_hash = :p, valid_after = :t, email_verified = :v "
                             "REMOVE pin_reset_hash, pin_reset_exp, failed_attempts, lockout_until, last_failed_at, "
                             "verify_token, verify_token_exp",
            ConditionExpression="pin_reset_hash = :h",                    # one use, even under a race
            ExpressionAttributeValues={":p": hash_pin(new_pin), ":t": now_ts, ":v": True, ":h": stored},
        )
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            return err(PIN_RESET_BAD_LINK_MSG, 400)
        print(f"[CW] reset_pin: {e}")
        return err("Could not reset the PIN. Please try again.", 500)
    revoke_sessions(email)
    _log_auth_event(event, email, True, "pin_reset")
    _send_pin_changed_email(email)
    return ok({"message": "PIN updated. Sign in with your new PIN."})

