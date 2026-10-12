"""lambda/handler.py — API Gateway Lambda router.

Thin routing layer only — no business logic lives here.
Lambda handler entry point: handler.handler

Architecture #1: module split complete.
Architecture #7: login() receives explicit event (no __event__ mutation).
"""
import urllib.parse
from helpers import ok, err, get_body, cors_headers

from auth     import register, login, logout, change_pin, verify_email, resend_verification
from prefs    import get_prefs, save_prefs, delete_account, unsubscribe, send_test_email, resend_weekly_email
from deals    import get_deals, search_stores, get_deal_history, get_deal_corpus, get_matches
from logging_utils import log_frontend_error
from inbound  import inbound_email_list
from admin    import (
    admin_login, admin_logout_session,
    admin_list_users, admin_create_user, admin_get_user, admin_delete_user,
    admin_reset_pin, admin_reset_email, admin_patch_prefs, admin_clear_items,
    admin_resend_verification,
    admin_stats, admin_inbound_logs,
    get_scrape_logs, invoke_scraper, get_log_tail,
    get_auth_logs, get_app_logs,
)


def handler(event, context):
    http_ctx = event.get("requestContext", {}).get("http", {})
    method   = (http_ctx.get("method") or event.get("httpMethod", "GET")).upper()
    path     = http_ctx.get("path") or event.get("rawPath") or event.get("path", "")
    body     = get_body(event)
    qs       = event.get("queryStringParameters") or {}

    # OPTIONS/preflight: API Gateway's CorsConfiguration adds CORS headers
    # (including MaxAge: 86400 from deploy.sh), but when CloudFront sits in
    # front the $default route still forwards OPTIONS to Lambda. We must
    # return 200 here — a 404 preflight kills the subsequent request.
    if method == "OPTIONS":
        hdrs = cors_headers(event)
        hdrs["Access-Control-Max-Age"] = "86400"
        return {"statusCode": 200, "headers": hdrs, "body": ""}

    # ── User / auth routes ────────────────────────────────────────────────────
    if path == "/auth/register"            and method == "POST":   return register(body, event)
    if path == "/auth/login"               and method == "POST":   return login(event, body)
    if path == "/auth/logout"              and method == "POST":   return logout(event)
    if path == "/auth/change-pin"          and method == "POST":   return change_pin(event, body)
    if path == "/auth/verify-email"        and method == "GET":    return verify_email(qs)
    if path == "/auth/resend-verification" and method == "POST":   return resend_verification(event, body)

    if path == "/user/prefs"       and method == "GET":    return get_prefs(event)
    if path == "/user/prefs"       and method == "PUT":    return save_prefs(event, body)
    if path == "/user/account"     and method == "DELETE": return delete_account(event)
    if path == "/user/unsubscribe" and method in ("GET", "POST"): return unsubscribe(event)
    if path == "/user/test-email"  and method == "POST":   return send_test_email(event)
    if path == "/user/resend-weekly" and method == "POST":  return resend_weekly_email(event)
    if path == "/user/matches"     and method == "GET":    return get_matches(event)

    if path == "/stores/search"    and method == "GET":    return search_stores(event)
    if path == "/deals"            and method == "GET":    return get_deals(event)
    if path == "/deals/history"    and method == "GET":    return get_deal_history(event)
    if path == "/deals/corpus"     and method == "GET":    return get_deal_corpus(event)

    if path == "/log/error"        and method == "POST":   return log_frontend_error(event, body)

    # ── Inbound email (Resend webhook) ────────────────────────────────────────
    if path == "/inbound/email-list" and method == "POST": return inbound_email_list(event)

    # ── Admin session auth (PDC-04) ───────────────────────────────────────────
    if path == "/admin/login"        and method == "POST": return admin_login(event, body)
    if path == "/admin/logout"       and method == "POST": return admin_logout_session(event)

    # ── Admin fixed routes ────────────────────────────────────────────────────
    if path == "/admin/users"        and method == "GET":  return admin_list_users(event)
    if path == "/admin/users"        and method == "POST": return admin_create_user(event, body)
    if path == "/admin/scrape-logs"  and method == "GET":  return get_scrape_logs(event)
    if path == "/admin/scrape-now"   and method == "POST": return invoke_scraper(event)
    if path == "/admin/logs/tail"    and method == "GET":  return get_log_tail(event)
    if path == "/admin/stats"        and method == "GET":  return admin_stats(event)
    if path == "/admin/auth-logs"    and method == "GET":  return get_auth_logs(event)
    if path == "/admin/app-logs"     and method == "GET":  return get_app_logs(event)
    if path == "/admin/inbound-logs" and method == "GET":  return admin_inbound_logs(event)

    # ── Admin parameterised routes  /admin/users/{email}[/action] ────────────
    if path.startswith("/admin/users/"):
        rest   = path[len("/admin/users/"):]
        parts  = rest.split("/", 1)
        uemail = urllib.parse.unquote(parts[0])
        action = parts[1] if len(parts) > 1 else ""
        if not action and method == "GET":      return admin_get_user(event, uemail)
        if not action and method == "DELETE":   return admin_delete_user(event, uemail)
        if action == "reset-pin"   and method == "POST": return admin_reset_pin(event, body, uemail)
        if action == "reset-email" and method == "POST": return admin_reset_email(event, body, uemail)
        if action == "prefs"       and method == "PUT":  return admin_patch_prefs(event, body, uemail)
        if action == "items"       and method == "DELETE": return admin_clear_items(event, uemail)
        if action == "resend-verification" and method == "POST": return admin_resend_verification(event, uemail)

    return err("Not found.", 404)
