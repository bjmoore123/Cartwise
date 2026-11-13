"""shared/unsub.py — the unsubscribe link that goes at the bottom of every Cartwise email (v9.11).

One HMAC-signed link per account (same token as /user/unsubscribe has always used), so it
works without signing in and whether or not the account has been verified.

Unsubscribing turns off everything Cartwise sends on its own schedule: weekly deal alerts
and verification reminders. Emails a person asks for (a PIN reset link, a fresh verification
link, a test email) and the "your PIN was changed" security notice still go out, and still
carry the link.
"""
import hashlib
import hmac
import urllib.parse

def token(secret: str, email: str) -> str:
    return hmac.new(secret.encode(), email.lower().strip().encode(), hashlib.sha256).hexdigest()


def url(api_url: str, secret: str, email: str) -> str:
    """Full unsubscribe link for an account, or "" if the server isn't configured for it."""
    secret = (secret or "").strip()
    if not secret or secret == "changeme-set-in-deploy" or not api_url or not email:
        return ""
    email = email.lower().strip()
    return f"{api_url.rstrip('/')}/user/unsubscribe?" + urllib.parse.urlencode(
        {"email": email, "token": token(secret, email)})


def headers(unsub_url: str) -> dict:
    """List-Unsubscribe (RFC 2369) + one-click (RFC 8058): the unsubscribe button mail apps show.
    POST /user/unsubscribe accepts the same query string."""
    if not unsub_url:
        return {}
    return {"List-Unsubscribe": f"<{unsub_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
