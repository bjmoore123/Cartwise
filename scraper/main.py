"""
scraper/main.py  (v6 - serverless Lambda)

Lambda entry point. Triggered by EventBridge weekly.
Writes a scrape-job summary record to DynamoDB after each run.
Uses per-user notify_email for alert delivery.

v6 additions:
  - history_deals()  — writes a dated weekly snapshot to deal-history table
"""

import json
import os
import boto3
import hmac
import hashlib
import resend
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import time
import secrets as _secrets

from scraper import get_deals_with_status
from shared import email_render
from shared.deal_parser import effective_bogo_mode
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
            "expires_at": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp()),  # v9.3
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

def _chunk_key(store_id: str, gen: str, i: int) -> str:
    """Same key scheme as lambda/deals.py _chunk_key (legacy rows have no gen)."""
    return f"{store_id}#{gen}#{i}" if gen else f"{store_id}#{i}"


def cache_deals(store_id: str, deals: list[dict]):
    """Write this week's deals for a store without a window where readers see nothing.

    v9.2.2 (#6): the old version deleted the previous chunks first, so a failure
    mid-write (or a reader in between) saw a partial or empty cache. Now:
      1. write new chunks under a fresh generation key  store_id#<gen>#N
      2. switch the index row to the new generation
      3. delete the previous generation's chunks (legacy store_id#N rows included)
    """
    now        = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    gen        = now.strftime("%Y%m%dT%H%M%S") + _secrets.token_hex(2)
    ttl_epoch  = int((now + timedelta(days=10)).timestamp())
    chunks = [deals[i:i+CHUNK_SIZE] for i in range(0, max(len(deals), 1), CHUNK_SIZE)]
    num_chunks = len(chunks)

    old_meta = {}
    try:
        old_meta = deals_table.get_item(Key={"store_id": store_id}).get("Item") or {}
    except Exception as e:
        print(f"  WARNING: could not read old cache index for {store_id}: {e}")

    # 1. New chunks (if this raises, the old cache is untouched)
    for i, chunk in enumerate(chunks):
        deals_table.put_item(Item={
            "store_id":    _chunk_key(store_id, gen, i),
            "gen":         gen,
            "fetched_at":  fetched_at,
            "deals":       json.dumps(chunk),
            "count":       len(chunk),
            "num_chunks":  num_chunks,
            "chunk_index": i,
            "expires_at":  ttl_epoch,
        })

    # 2. Switch the index row (no deals, just metadata) to the new generation
    deals_table.put_item(Item={
        "store_id":   store_id,
        "fetched_at": fetched_at,
        "count":      len(deals),
        "num_chunks": num_chunks,
        "gen":        gen,
        "expires_at": ttl_epoch,
    })

    # 3. Remove the previous generation (best effort; TTL cleans up leftovers)
    old_gen = str(old_meta.get("gen", "") or "")
    if old_meta and old_gen != gen:
        for i in range(int(old_meta.get("num_chunks", 0))):
            try:
                deals_table.delete_item(Key={"store_id": _chunk_key(store_id, old_gen, i)})
            except Exception as e:
                print(f"  WARNING: could not delete old chunk {i} for {store_id}: {e}")


def _parse_valid_date(vf: str, today: date):
    """Publix valid dates: "10/7" (no year) or older "Oct 7, 2026" / "2026-10-07" / "10/7/2026".
    For "M/D" the year is the one that puts the date nearest to today (handles Dec→Jan)."""
    vf = (vf or "").strip()
    if not vf or vf in ("1/1", "01/01"):           # TPR / coupon placeholder
        return None
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(vf, fmt).date()
        except ValueError:
            pass
    try:
        m, d = (int(x) for x in vf.split("/")[:2])
        best = None
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                cand = date(y, m, d)
            except ValueError:
                continue
            if best is None or abs((cand - today).days) < abs((best - today).days):
                best = cand
        return best
    except (ValueError, TypeError):
        return None


def _week_start(deals: list[dict], today: date | None = None) -> str:
    """Monday of the ad week, from the weekly ad's valid_from dates (v9.6).

    The old version only parsed formats Publix no longer sends, looked at the first
    deal (often a TPR with a "1/1" placeholder) and fell back to "this Monday". A
    manual scrape on Monday or Tuesday therefore filed LAST week's ad under the new
    week. Now: the most common real valid_from among weekly-ad deals decides.
    """
    from collections import Counter
    today = today or date.today()
    votes = Counter()
    for d in deals:
        if (d.get("saving_type") or "WeeklyAd") not in ("WeeklyAd", "Stacked", "StackedDeals", "ExtraSavings"):
            continue
        dt = _parse_valid_date(d.get("valid_from", ""), today)
        if dt:
            votes[dt] += 1
    if not votes:                               # nothing usable: any deal with a real date
        for d in deals:
            dt = _parse_valid_date(d.get("valid_from", ""), today)
            if dt:
                votes[dt] += 1
    start = votes.most_common(1)[0][0] if votes else today
    monday = start - timedelta(days=start.weekday())
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
        # v9.4: what history needs to chart and compare real prices
        "final_price": d.get("final_price") or 0,
        "save_line":   d.get("save_line", ""),
        **({"regular_price": d["regular_price"]} if d.get("regular_price") else {}),
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

    # v9.2.1: invalidate the API's persisted history summary so the next
    # /deals/history request rebuilds it with this week's snapshot.
    try:
        history_tbl.delete_item(Key={"store_id": store_id, "sk": "_summary"})
    except Exception as e:
        print(f"  WARNING: could not invalidate history summary for {store_id}: {e}")

    print(f"  History snapshot written: {store_id}/{week_start} ({len(slim_deals)} deals, {num_chunks} chunk(s))")


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

def build_html(matches, store_id, store_name, frontend_url="", account_email="", bogo_mode="two"):
    """v9.3: rendered by shared/email_render.py from the normalized offer."""
    unsub_url = _unsub_url(account_email) if account_email else ""
    return email_render.build_html(matches, store_id, store_name, frontend_url,
                                   unsub_url=unsub_url, account_email=account_email,
                                   bogo_mode=bogo_mode)


def send_alert(notify_email: str, matches: list[dict], store_id: str, store_name: str,
               account_email: str = "", bogo_mode: str = "two"):
    subject   = email_render.subject_line(matches)
    unsub_url = _unsub_url(account_email) if account_email else ""
    msg = {
        "from":    RESEND_FROM,
        "to":      [notify_email],
        "subject": subject,
        "html":    build_html(matches, store_id, store_name, FRONTEND_URL, account_email=account_email,
                              bogo_mode=bogo_mode),
        "text":    email_render.build_text(matches, store_name, unsub_url, bogo_mode=bogo_mode),
    }
    if unsub_url:
        # v9.3: one-click unsubscribe (RFC 8058); POST /user/unsubscribe accepts the same query
        msg["headers"] = {
            "List-Unsubscribe":      f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    try:
        resend.Emails.send(msg)
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
            try:                                    # v9.5: count in Admin "Users Deleted"
                users_table.update_item(
                    Key={"email": "__meta__"},
                    UpdateExpression="ADD deleted_count :one SET last_deleted_at = :ts",
                    ExpressionAttributeValues={":one": 1, ":ts": datetime.now(timezone.utc).isoformat()},
                )
            except Exception as e:
                print(f"  WARNING: deleted_count update failed: {e}")
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
                "expires_at": int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),  # v9.3
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
            "expires_at":     int((datetime.now(timezone.utc) + timedelta(days=90)).timestamp()),  # v9.3
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


FETCH_RETRY_DELAY_S = int(os.environ.get("FETCH_RETRY_DELAY_S", "20"))


def _log_scrape_problem(store_id: str, store_name: str, message: str, **fields):
    """app_logs entry (source=scraper) so a skipped store shows up in Admin → Logs."""
    try:
        now = datetime.now(timezone.utc)
        app_logs_tbl.put_item(Item={
            "log_id":     f"{now.isoformat()}#{_secrets.token_hex(4)}",
            "ts":         now.isoformat(),
            "source":     "scraper",
            "level":      "error",
            "store_id":   str(store_id),
            "store_name": store_name or "",
            "message":    message[:300],
            "expires_at": int((now + timedelta(days=30)).timestamp()),
            **{k: v for k, v in fields.items() if v is not None},
        })
    except Exception as e:
        print(f"  WARNING: could not write scraper log: {e}")


def fetch_deals_safely(store_id: str):
    """Fetch deals, retrying once. Returns (deals, problem); problem is "" when the data is usable.

    Usable means every source answered and at least one deal came back. Anything
    else keeps last week's cache, history and corpus as they are (v9.2.2 #6).
    """
    problem = ""
    for attempt in (1, 2):
        try:
            deals, failed = get_deals_with_status(store_id)
        except Exception as e:
            deals, failed = [], [f"exception: {e}"]
        if failed:
            problem = f"source(s) failed: {', '.join(failed)} ({len(deals)} deals from the rest)"
        elif not deals:
            problem = "Publix returned 0 deals"
        else:
            return deals, ""
        print(f"  attempt {attempt}: {problem}")
        if attempt == 1 and FETCH_RETRY_DELAY_S > 0:
            time.sleep(FETCH_RETRY_DELAY_S)
    return deals, problem


def _store_worker(event):
    """Process a single store: fetch deals, cache, write history, match users, send emails."""
    store_id    = event["store_id"]
    store_name  = event.get("store_name", "")
    store_users = event.get("users", [])
    send_emails = event.get("send_emails", False)
    job_id      = event.get("job_id", "")

    print(f"▶ Worker: {store_name or f'Store #{store_id}'} — {len(store_users)} user(s)")

    deals, problem = fetch_deals_safely(store_id)
    if problem:
        # Keep the last good cache: no cache/history/corpus writes and no emails
        # built from partial data. Admin can re-run or resend once Publix recovers.
        msg = f"Skipped store {store_id}: {problem}. Kept the previous cache; no emails sent."
        print(f"  ERROR: {msg}")
        _log_scrape_problem(store_id, store_name, msg, job_id=job_id or None,
                            deal_count=len(deals), users=len(store_users))
        return
    print(f"  {len(deals)} deals fetched")

    try:
        cache_deals(store_id, deals)
    except Exception as e:
        print(f"  WARNING: cache failed: {e}")

    try:
        week_start = _week_start(deals)
        history_deals(store_id, deals, week_start)
    except Exception as e:
        print(f"  WARNING: history write failed: {e}")

    # v9.8.4: autocomplete now comes from the 52-week history index; the global
    # corpus row is no longer written (it hit the 400 KB item limit on 2026-04-08).

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
                send_alert(notify_email, matches, store_id, store_name, account_email=account_email,
                           bogo_mode=effective_bogo_mode(prefs))   # v9.8: store's BOGO rule
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

    # v9.8.1: workers run on THIS function. The scraper Lambda never had
    # SCRAPER_FUNCTION set, so staging fell back to "cartwise-scraper" (production)
    # and IAM refused it. AWS_LAMBDA_FUNCTION_NAME is always set by Lambda.
    scraper_fn = (os.environ.get("SCRAPER_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
                  or "cartwise-scraper")
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
    sent = sum(1 for d in job["store_details"] if d.get("status") == "dispatched")
    print(f"\nOrchestrator done. Mode: {mode}. {sent} of {len(by_store)} workers dispatched.")
