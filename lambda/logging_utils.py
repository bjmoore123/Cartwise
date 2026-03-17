"""lambda/logging_utils.py — Auth-event and application-event logging.

Architecture #9: geo-lookup removed from login hot path. Auth logs store raw IP only.
Architecture #3: no silent exception swallowing.
PDC-11: log_frontend_error requires session auth.
PDC-20: TTL fields on auth-logs (90 days) and app-logs (30 days).
"""
import secrets
import re
from datetime import datetime, timezone, timedelta

from helpers import (
    _get_client_ip, get_session,
    auth_logs_tbl, app_logs_tbl,
    ok, err,
    CORS,
)

_VALID_LOG_LEVELS = {"error", "warn", "info"}


def _log_auth_event(event: dict, email: str, success: bool, reason: str = ""):
    """Write a login attempt record to auth_logs.
    Geo-IP from CloudFront-Viewer-Country/City edge headers (zero latency, no
    external call). Fields present only when CloudFront is in front (Phase 3+);
    gracefully absent otherwise."""
    try:
        ip      = _get_client_ip(event)
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        ua      = headers.get("user-agent", "")[:200]
        country = headers.get("cloudfront-viewer-country", "")[:4]   # ISO 3166-1 alpha-2
        city    = headers.get("cloudfront-viewer-city", "")[:100]
        now     = datetime.now(timezone.utc)
        ttl     = int((now + timedelta(days=90)).timestamp())   # PDC-20: 90-day TTL
        item = {
            "log_id":     f"{now.isoformat()}#{secrets.token_hex(4)}",
            "ts":         now.isoformat(),
            "email":      email or "(unknown)",
            "success":    success,
            "ip":         ip,
            "user_agent": ua,
            "reason":     reason,
            "expires_at": ttl,
        }
        if country: item["country"] = country
        if city:    item["city"]    = city
        auth_logs_tbl.put_item(Item=item)
    except Exception as e:
        print(f"[CW] _log_auth_event failed: {e}")  # never let logging break auth


def _log_app_event(source: str, level: str = "info", **fields):
    """Write a structured application log entry to app_logs.
    source: 'frontend' | 'api' | 'email' | 'cache'
    Never raises — logging must never break the main request flow."""
    try:
        now = datetime.now(timezone.utc)
        ttl = int((now + timedelta(days=30)).timestamp())   # PDC-20: 30-day TTL
        item = {
            "log_id":     f"{now.isoformat()}#{secrets.token_hex(4)}",
            "ts":         now.isoformat(),
            "source":     source,
            "level":      level,
            "expires_at": ttl,   # attribute name matches deploy.sh TTL config
            **{k: v for k, v in fields.items() if v is not None},
        }
        app_logs_tbl.put_item(Item=item)
    except Exception as e:
        print(f"[CW] _log_app_event failed: {e}")


def log_frontend_error(event, body):
    """POST /log/error — receive frontend JS error reports.
    PDC-11: requires session auth; log level validated against allowlist."""
    sess = get_session(event)
    if not sess: return err("Not authenticated.", 401)
    level = body.get("level", "error")
    if level not in _VALID_LOG_LEVELS:
        level = "error"
    try:
        ip  = _get_client_ip(event)
        now = datetime.now(timezone.utc).isoformat()
        ttl = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        app_logs_tbl.put_item(Item={
            "log_id":     f"{now}#{secrets.token_hex(4)}",
            "ts":         now,
            "source":     "frontend",
            "level":      level,
            "message":    str(body.get("message", ""))[:500],
            "stack":      str(body.get("stack", ""))[:1000],
            "url":        str(body.get("url", ""))[:200],
            "ip":         ip,
            "user":       sess.get("email", ""),
            "expires_at": ttl,
        })
        return ok({"message": "Logged."})
    except Exception as e:
        print(f"[CW] log_frontend_error write failed: {e}")
        return err("Log write failed.", 500)
