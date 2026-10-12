"""
scraper/main.py  (v6 - serverless Lambda)

Lambda entry point. Triggered by EventBridge weekly.
Writes a scrape-job summary record to DynamoDB after each run.
Uses per-user notify_email for alert delivery.

v6 additions:
  - history_deals()  — writes a dated weekly snapshot to deal-history table
  - update_corpus()  — merges deal titles/brands into global corpus for autocomplete
"""

import json
import os
import boto3
import hmac
import hashlib
import resend
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

from scraper import get_deals
from shared.matcher import match_deals_for_user

# ── Config ────────────────────────────────────────────────────────────────────

AWS_REGION      = os.environ.get("PDC_REGION", os.environ.get("AWS_REGION", "us-east-1"))
USERS_TABLE     = os.environ["USERS_TABLE"]
DEALS_TABLE     = os.environ["DEALS_TABLE"]
SCRAPE_LOGS_TBL = os.environ.get("SCRAPE_LOGS_TABLE", "cartwise-scrape-logs")
HISTORY_TABLE   = os.environ.get("HISTORY_TABLE", "")
CORPUS_TABLE    = os.environ.get("CORPUS_TABLE", "")

resend.api_key  = os.environ["RESEND_API_KEY"]
FRONTEND_URL    = os.environ.get("FRONTEND_URL", "")
API_URL         = os.environ.get("API_URL", "")        # base URL for unsubscribe endpoint
UNSUB_SECRET    = (os.environ.get("UNSUB_SECRET", "") or "").strip()
RESEND_FROM    = (
    f"{os.environ.get('RESEND_FROM_NAME','Cartwise Alerts')} "
    f"<{os.environ.get('RESEND_FROM_ADDR','onboarding@resend.dev')}>"
)

dynamodb        = boto3.resource("dynamodb", region_name=AWS_REGION)
users_table     = dynamodb.Table(USERS_TABLE)
deals_table     = dynamodb.Table(DEALS_TABLE)
scrape_logs_tbl = dynamodb.Table(SCRAPE_LOGS_TBL)
APP_LOGS_TBL    = os.environ.get("APP_LOGS_TABLE", "cartwise-app-logs")
app_logs_tbl    = dynamodb.Table(APP_LOGS_TBL)


# ── DynamoDB helpers ──────────────────────────────────────────────────────────

def to_python(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):  return {k: to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [to_python(i) for i in obj]
    return obj


def _log_email(to: str, subject: str, ok: bool, user: str = "", message: str = ""):
    """Log an email delivery attempt to app_logs DynamoDB table."""
    try:
        import secrets as _sec
        now = datetime.now(timezone.utc).isoformat()
        app_logs_tbl.put_item(Item={
            "log_id":  f"{now}#{_sec.token_hex(4)}",
            "ts":      now,
            "source":  "email",
            "level":   "info" if ok else "error",
            "ok":      ok,
            "to":      to,
            "subject": subject[:120],
            "trigger": "scheduled",
            "user":    user,
            "message": message[:200],
        })
    except Exception:
        pass


def get_all_users() -> list[dict]:
    items, resp = [], users_table.scan(ProjectionExpression="email, prefs, email_verified")
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = users_table.scan(
            ProjectionExpression="email, prefs, email_verified",
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return [to_python(u) for u in items]


CHUNK_SIZE = 200  # deals per DynamoDB row (~200 rows * ~200 bytes = ~40KB per chunk, well under 400KB)

def cache_deals(store_id: str, deals: list[dict]):
    fetched_at = datetime.now(timezone.utc).isoformat()
    ttl_epoch  = int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp())
    chunks = [deals[i:i+CHUNK_SIZE] for i in range(0, max(len(deals), 1), CHUNK_SIZE)]
    num_chunks = len(chunks)

    # Delete stale chunks from previous runs before writing new ones.
    # Read the old num_chunks from the index row to know how many to delete.
    try:
        old_meta = deals_table.get_item(Key={"store_id": store_id}).get("Item") or {}
        old_num_chunks = int(old_meta.get("num_chunks", 0))
        for i in range(old_num_chunks):
            deals_table.delete_item(Key={"store_id": f"{store_id}#{i}"})
    except Exception as e:
        print(f"  WARNING: could not delete old chunks for {store_id}: {e}")

    # Write each chunk as store_id#N
    for i, chunk in enumerate(chunks):
        deals_table.put_item(Item={
            "store_id":    f"{store_id}#{i}",
            "fetched_at":  fetched_at,
            "deals":       json.dumps(chunk),
            "count":       len(chunk),
            "num_chunks":  num_chunks,
            "chunk_index": i,
            "expires_at":  ttl_epoch,
        })

    # Write an index row at store_id (no deals, just metadata)
    deals_table.put_item(Item={
        "store_id":   store_id,
        "fetched_at": fetched_at,
        "count":      len(deals),
        "num_chunks": num_chunks,
        "expires_at": ttl_epoch,
    })


def _week_start(deals: list[dict]) -> str:
    """
    Derive the Monday of the current ad week from the first deal's valid_from date.
    Falls back to the Monday of today if no valid_from is present or parseable.
    """
    for d in deals:
        vf = d.get("valid_from", "")
        if vf:
            try:
                # valid_from may be "Jan 23, 2026" or "2026-01-23" — try both
                for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
                    try:
                        dt = datetime.strptime(vf.strip(), fmt)
                        # Roll back to Monday
                        monday = dt - timedelta(days=dt.weekday())
                        return monday.strftime("%Y-%m-%d")
                    except ValueError:
                        continue
            except Exception:
                pass
    # Fallback: Monday of current week
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%Y-%m-%d")


def _slim_deal(d: dict) -> dict:
    """Return a lightweight deal shape for history storage (strips large fields)."""
    return {
        "id":          d.get("id", ""),
        "title":       d.get("title", ""),
        "savings":     d.get("savings", ""),
        "saving_type": d.get("saving_type", ""),
        "is_bogo":     d.get("is_bogo", False),
        "department":  d.get("department", ""),
        "brand":       d.get("brand", ""),
    }


def history_deals(store_id: str, deals: list[dict], week_start: str):
    """
    Write a weekly snapshot of deals to the deal-history table.
    Composite key: PK=store_id (just store number), SK=week or week#chunk_index.
    Key examples: PK="1546" SK="2026-03-16" (index), SK="2026-03-16#0" (chunk).
    TTL: 52 weeks from now.
    """
    if not HISTORY_TABLE:
        return

    history_tbl = dynamodb.Table(HISTORY_TABLE)
    slim_deals  = [_slim_deal(d) for d in deals]
    fetched_at  = datetime.now(timezone.utc).isoformat()
    ttl_epoch   = int((datetime.now(timezone.utc) + timedelta(weeks=52)).timestamp())

    chunks     = [slim_deals[i:i+CHUNK_SIZE] for i in range(0, max(len(slim_deals), 1), CHUNK_SIZE)]
    num_chunks = len(chunks)

    # Delete old chunks for this store+week before writing (prevents stale accumulation)
    try:
        from boto3.dynamodb.conditions import Key as DKey
        old = history_tbl.query(
            KeyConditionExpression=DKey("store_id").eq(store_id) & DKey("sk").begins_with(f"{week_start}"),
            ProjectionExpression="store_id, sk",
        )
        for item in old.get("Items", []):
            history_tbl.delete_item(Key={"store_id": item["store_id"], "sk": item["sk"]})
    except Exception:
        pass  # table may be empty or old schema — safe to ignore

    for i, chunk in enumerate(chunks):
        history_tbl.put_item(Item={
            "store_id":    store_id,
            "sk":          f"{week_start}#{i}",
            "fetched_at":  fetched_at,
            "deals":       json.dumps(chunk),
            "count":       len(chunk),
            "num_chunks":  num_chunks,
            "chunk_index": i,
            "expires_at":  ttl_epoch,
        })

    # Index row — no deals, just metadata
    history_tbl.put_item(Item={
        "store_id":   store_id,
        "sk":         week_start,
        "week":       week_start,
        "fetched_at": fetched_at,
        "count":      len(slim_deals),
        "num_chunks": num_chunks,
        "expires_at": ttl_epoch,
    })

    print(f"  History snapshot written: {store_id}/{week_start} ({len(slim_deals)} deals, {num_chunks} chunk(s))")


def update_corpus(deals: list[dict]):
    """
    Merge deal titles and brand strings from this week's deals into the
    global corpus row (key="global") using DynamoDB ADD on a StringSet.
    The corpus feeds the List tab autocomplete with cross-week deal names.
    No TTL — corpus grows indefinitely, never shrinks.
    """
    if not CORPUS_TABLE:
        return

    corpus_tbl = dynamodb.Table(CORPUS_TABLE)

    # Collect all non-empty titles and brands
    new_terms = set()
    for d in deals:
        title = (d.get("title") or "").strip()
        brand = (d.get("brand") or "").strip()
        if title:
            new_terms.add(title)
        if brand:
            new_terms.add(brand)

    if not new_terms:
        return

    try:
        corpus_tbl.update_item(
            Key={"corpus_id": "global"},
            UpdateExpression="ADD terms :t SET updated_at = :u",
            ExpressionAttributeValues={
                ":t": new_terms,
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"  Corpus updated: {len(new_terms)} term(s) merged")
    except Exception as e:
        print(f"  WARNING: corpus update failed: {e}")


def write_scrape_log(log: dict):
    """Persist scrape job summary to DynamoDB with a 1-year TTL."""
    try:
        log["expires_at"] = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())
        scrape_logs_tbl.put_item(Item=log)
    except Exception as e:
        print(f"WARNING: failed to write scrape log: {e}")


# ── Email ─────────────────────────────────────────────────────────────────────

def _unsub_token(email: str) -> str:
    """HMAC-SHA256 of email — matches api.py logic. Returns hex string."""
    return hmac.new(
        UNSUB_SECRET.encode(),
        email.lower().strip().encode(),
        hashlib.sha256,
    ).hexdigest()

def _unsub_url(account_email: str) -> str:
    """Build the full unsubscribe URL for a given account email."""
    if not UNSUB_SECRET or not API_URL:
        return ""
    import urllib.parse
    token = _unsub_token(account_email)
    params = urllib.parse.urlencode({"email": account_email, "token": token})
    return f"{API_URL}/user/unsubscribe?{params}"

def build_html(matches, store_id, store_name, frontend_url="", account_email=""):
    today = date.today().strftime("%B %d, %Y")
    rows  = ""
    for m in matches:
        valid = ""
        if m.get("valid_from") or m.get("valid_thru"):
            valid = f'<div style="font-size:11px;color:#999;">Valid {m.get("valid_from","")}–{m.get("valid_thru","")}</div>'
        coupon_link = ""
        if m.get("coupon_id"):
            coupon_link = f'<div><a href="https://www.publix.com/savings/digital-coupons?cid={m["coupon_id"]}" style="font-size:11px;color:#8b5cf6;">✂️ Clip Coupon</a></div>'
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:10px 8px;width:40px;text-align:center;font-size:22px;">🛒</td>
          <td style="padding:10px 8px;">
            <div style="font-size:10px;font-weight:700;color:#1a6b3c;text-transform:uppercase;letter-spacing:.07em;margin-bottom:2px;">{m.get('my_item','')}</div>
            <div style="font-weight:600;">{m['deal_name']}</div>
            {f'<div style="font-size:12px;color:#555;">{m["description"]}</div>' if m.get('description') else ''}
            {f'<div style="font-size:11px;color:#999;">{m["fine_print"]}</div>' if m.get('fine_print') else ''}
            {valid}
            {coupon_link}
          </td>
          <td style="padding:10px 8px;text-align:right;white-space:nowrap;">
            <div style="font-size:20px;font-weight:700;color:#1a6b3c;">{m.get('savings','')}</div>
            {f'<div style="font-size:12px;color:#e76f51;">{m["save_line"]}</div>' if m.get('save_line') else ''}
          </td>
        </tr>"""

    unsub_url = _unsub_url(account_email) if account_email else ""
    unsub_section = (
        f'<div style="margin-top:8px;font-size:12px;color:#aaa;">'
        f'Don\'t want these alerts? '
        f'<a href="{unsub_url}" style="color:#aaa;">Unsubscribe</a>'
        f'</div>'
    ) if unsub_url else ""

    return f"""<html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;color:#333;">
      <div style="background:#1a6b3c;color:white;padding:20px 24px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;">&#x1F6D2; Publix Deal Alert</h2>
        <p style="margin:4px 0 0;opacity:.85;">{today} &nbsp;&middot;&nbsp; {store_name or f'Store #{store_id}'} &nbsp;&middot;&nbsp; {len(matches)} item(s) on sale</p>
      </div>
      <table style="width:100%;border-collapse:collapse;background:white;border:1px solid #ddd;border-top:none;">
        <thead><tr style="background:#f8f8f8;font-size:11px;color:#888;text-transform:uppercase;">
          <th style="padding:8px;"></th>
          <th style="padding:8px;text-align:left;">Item</th>
          <th style="padding:8px;text-align:right;">Price</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="padding:16px;background:#f0f7f3;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;text-align:center;">
        <a href="{frontend_url or 'https://www.publix.com/savings/weekly-ad/view-all'}"
           style="background:#1a6b3c;color:white;padding:10px 24px;border-radius:4px;text-decoration:none;font-weight:600;">
          View My Matches &#x2197;
        </a>
        {unsub_section}
      </div>
    </body></html>"""


def send_alert(notify_email: str, matches: list[dict], store_id: str, store_name: str,
               account_email: str = ""):
    today   = date.today().strftime("%B %d")
    subject = f"🛒 Publix Deals — {len(matches)} item(s) on sale ({today})"
    try:
        resend.Emails.send({
            "from":    RESEND_FROM,
            "to":      [notify_email],
            "subject": subject,
            "html":    build_html(matches, store_id, store_name, FRONTEND_URL, account_email=account_email),
        })
        _log_email(notify_email, subject, ok=True, user=account_email)
        print(f"   Email sent → {notify_email}")
    except Exception as exc:
        _log_email(notify_email, subject, ok=False, user=account_email, message=str(exc)[:200])
        print(f"   Email FAILED → {notify_email}: {exc}")
        raise

# ── Lambda handler ────────────────────────────────────────────────────────────

def purge_inactive_users() -> None:
    """Monthly cleanup: delete accounts created > 30 days ago that have
    no store selected OR an empty shopping list. These are users who signed
    up but never completed setup and are no longer active.

    Logs each deletion and a summary to app_logs.
    """
    import secrets as _sec
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    print(f"\nMonthly cleanup — cutoff: {cutoff[:10]}")
    print("=" * 50)

    # Full scan; users table is small
    items, resp = [], users_table.scan(
        ProjectionExpression="email, created_at, prefs",
    )
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = users_table.scan(
            ProjectionExpression="email, created_at, prefs",
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))

    users = [to_python(u) for u in items]
    deleted, skipped = [], []

    for u in users:
        email      = u.get("email", "")
        created_at = u.get("created_at", "")
        prefs      = u.get("prefs") or {}

        # Skip the metadata sentinel row if present
        if email == "__meta__":
            continue

        # Only target accounts older than 30 days
        if not created_at or created_at >= cutoff:
            continue

        store_id = (prefs.get("store_id") or "").strip()
        items_list = prefs.get("items") or []
        incomplete = (not store_id) or (len(items_list) == 0)

        if not incomplete:
            continue

        reason = []
        if not store_id:    reason.append("no store")
        if not items_list:  reason.append("empty list")

        try:
            users_table.delete_item(Key={"email": email})
            deleted.append(email)
            print(f"  Deleted: {email} ({', '.join(reason)}, created {created_at[:10]})")

            now = datetime.now(timezone.utc).isoformat()
            app_logs_tbl.put_item(Item={
                "log_id":  f"{now}#{_sec.token_hex(4)}",
                "ts":      now,
                "source":  "api",
                "level":   "info",
                "action":  "cleanup-user-deleted",
                "message": f"Inactive account deleted: {email} ({', '.join(reason)})",
                "user":    email,
            })
        except Exception as e:
            skipped.append(email)
            print(f"  ERROR deleting {email}: {e}")

    summary = f"Cleanup complete. Deleted: {len(deleted)}, Errors: {len(skipped)}"
    print(f"\n{summary}")

    # Log summary
    try:
        now = datetime.now(timezone.utc).isoformat()
        app_logs_tbl.put_item(Item={
            "log_id":         f"{now}#{_sec.token_hex(4)}",
            "ts":             now,
            "source":         "api",
            "level":          "info",
            "action":         "cleanup-summary",
            "message":        summary,
            "deleted_count":  len(deleted),
            "error_count":    len(skipped),
        })
    except Exception:
        pass


def handler(event, context):
    """Lambda entry point -- called by EventBridge, admin, or self-invoked as store worker.

    Invocation modes:
      1. EventBridge weekly rule     -> ORCHESTRATOR: scan users, fan-out per-store workers
      2. Admin manual trigger        -> ORCHESTRATOR: same fan-out, but send_emails=False
      3. source=store-worker         -> WORKER: process one store (fetch, match, email)
      4. source=manual-resend        -> scrape + email one user only
      5. source=monthly-cleanup      -> delete inactive accounts
    """
    source       = event.get("source", "")
    detail_type  = event.get("detail-type", "")
    target_email = (event.get("target_email") or "").strip().lower()

    # Monthly inactive-account cleanup
    if source == "monthly-cleanup":
        purge_inactive_users()
        return {"statusCode": 200, "body": "Cleanup done."}

    # Per-store worker — invoked by orchestrator
    if source == "store-worker":
        _store_worker(event)
        return {"statusCode": 200, "body": "Store worker done."}

    # Explicit resend for a single user (no fan-out, runs inline)
    if source == "manual-resend" and target_email:
        users = get_all_users()
        user = next((u for u in users if u.get("email", "").lower() == target_email), None)
        if user:
            sid = (user.get("prefs") or {}).get("store_id", "")
            if sid:
                _store_worker({
                    "source": "store-worker",
                    "store_id": sid,
                    "store_name": (user.get("prefs") or {}).get("store_name", ""),
                    "users": [user],
                    "send_emails": True,
                })
        return {"statusCode": 200, "body": "Done (single-user resend)."}

    # Scheduled EventBridge trigger or admin manual -> orchestrator fan-out
    send_emails = (source == "aws.events") or (detail_type == "Scheduled Event")
    _orchestrator(send_emails=send_emails)
    return {"statusCode": 200, "body": "Done (orchestrator)."}


def _store_worker(event):
    """Process a single store: fetch deals, cache, write history, match users, send emails."""
    store_id    = event["store_id"]
    store_name  = event.get("store_name", "")
    store_users = event.get("users", [])
    send_emails = event.get("send_emails", False)
    job_id      = event.get("job_id", "")

    print(f"▶ Worker: {store_name or f'Store #{store_id}'} — {len(store_users)} user(s)")

    try:
        deals = get_deals(store_id)
        print(f"  {len(deals)} deals fetched")
    except Exception as e:
        print(f"  ERROR fetching deals: {e}")
        return

    try:
        cache_deals(store_id, deals)
    except Exception as e:
        print(f"  WARNING: cache failed: {e}")

    try:
        week_start = _week_start(deals)
        history_deals(store_id, deals, week_start)
    except Exception as e:
        print(f"  WARNING: history write failed: {e}")

    try:
        update_corpus(deals)
    except Exception as e:
        print(f"  WARNING: corpus update failed: {e}")

    emails_sent = 0
    for user in store_users:
        account_email  = user["email"]
        prefs          = user.get("prefs") or {}
        notify_email   = (prefs.get("notify_email") or account_email).strip()
        email_enabled  = prefs.get("email_enabled", True)
        email_verified = user.get("email_verified", True)
        items          = prefs.get("items") or []
        matching       = prefs.get("matching") or {}

        if not items:
            continue

        matches = match_deals_for_user(deals, items, matching)
        if not matches:
            continue

        if not send_emails:
            print(f"    {len(matches)} match(es) for {account_email} — skipping (manual run)")
        elif not email_verified:
            print(f"    {len(matches)} match(es) for {account_email} — skipping (not verified)")
        elif not email_enabled:
            print(f"    {len(matches)} match(es) for {account_email} — skipping (opted out)")
        else:
            try:
                send_alert(notify_email, matches, store_id, store_name, account_email=account_email)
                emails_sent += 1
            except Exception as e:
                print(f"    ERROR sending email to {notify_email}: {e}")

    print(f"  Worker done: {emails_sent} email(s) sent")


def _orchestrator(send_emails: bool = False):
    """Fan-out: scan all users, group by store, invoke one worker Lambda per store."""
    started_at = datetime.now(timezone.utc)
    print(f"\nCartwise Orchestrator — {date.today()}")
    print("=" * 50)

    job = {
        "job_id":          started_at.strftime("%Y%m%d-%H%M%S"),
        "started_at":      started_at.isoformat(),
        "finished_at":     "",
        "stores_examined": 0,
        "total_deals":     0,
        "emails_sent":     0,
        "total_users":     0,
        "errors":          [],
        "store_details":   [],
        "mode":            "fan-out",
    }

    users = get_all_users()
    print(f"Loaded {len(users)} registered user(s)")
    if not users:
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_scrape_log(job)
        return

    by_store: dict[str, list[dict]] = {}
    for u in users:
        sid = (u.get("prefs") or {}).get("store_id", "")
        if sid:
            by_store.setdefault(sid, []).append(u)

    if not by_store:
        print("No users have a store selected — nothing to do.")
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_scrape_log(job)
        return

    job["stores_examined"] = len(by_store)
    job["total_users"]     = sum(len(v) for v in by_store.values())
    print(f"Fanning out to {len(by_store)} store worker(s) for {job['total_users']} user(s)...\n")

    scraper_fn = os.environ.get("SCRAPER_FUNCTION", "cartwise-scraper")
    lambda_cli = boto3.client("lambda", region_name=AWS_REGION)

    for store_id, store_users in by_store.items():
        prefs0     = store_users[0].get("prefs") or {}
        store_name = prefs0.get("store_name") or ""

        # Build lightweight user list for the worker (only fields it needs)
        slim_users = []
        for u in store_users:
            slim_users.append({
                "email":          u["email"],
                "email_verified": u.get("email_verified", True),
                "prefs":          u.get("prefs") or {},
            })

        payload = {
            "source":      "store-worker",
            "store_id":    store_id,
            "store_name":  store_name,
            "users":       slim_users,
            "send_emails": send_emails,
            "job_id":      job["job_id"],
        }

        try:
            lambda_cli.invoke(
                FunctionName=scraper_fn,
                InvocationType="Event",   # async — don't wait for response
                Payload=json.dumps(payload),
            )
            print(f"  ▶ {store_name or store_id}: dispatched ({len(slim_users)} user(s))")
            job["store_details"].append({
                "store_id": store_id, "store_name": store_name,
                "users": len(slim_users), "status": "dispatched",
            })
        except Exception as e:
            msg = f"ERROR dispatching worker for {store_id}: {e}"
            print(f"  {msg}")
            job["errors"].append(msg)

    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_scrape_log(job)
    mode = "scheduled (emails enabled)" if send_emails else "manual (no emails)"
    print(f"\nOrchestrator done. Mode: {mode}. {len(by_store)} workers dispatched.")
