"""lambda/timeline.py — one person's history, and the one-time step that preserves it (v9.14).

    GET /admin/users/{email}/timeline     everything we know about one account, newest first

Where each part comes from, and why it's complete from v9.14 on:
  signed up / verified / alerts off+on / spam / bounce   fields on the account (kept forever)
  days on the site                                       account.site_day_list (v9.14; one entry per day)
  sign-ins, wrong PINs, lockouts, PIN resets             account.auth_history (v9.14; the 90-day log copied once)
  every email, delivered / opened / clicked              metrics em#<id> rows, kept 3 years (v9.14)
  emails sent before per-account email records began     metrics emlog#<id>, copied once from the 30-day app log
  feedback                                               feedback table (kept 1 year by design)

History from before tracking began can't be rebuilt. migrate() runs once per account (deploy.sh
calls it after every deploy; accounts already done are skipped). It copies what the short-lived
logs still hold onto the account, and writes `history_gaps` on the account: one plain-English
note per kind of history that's missing, so the gap is recorded on the data itself.
"""
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from helpers import (ok, err, _to_py, _scan_all, users_table, auth_logs_tbl, app_logs_tbl, dynamodb)
from logging_utils import auth_history_entry, AUTH_HISTORY_MAX
from shared import mailmeta
from shared.usage import parse, day_key, MAX_DAYS
import segments

metrics_tbl = dynamodb.Table(os.environ.get("METRICS_TABLE", "cartwise-metrics"))
feedback_tbl = dynamodb.Table(os.environ.get("FEEDBACK_TABLE", "cartwise-feedback"))

# When each kind of tracking reached production (release commit times; deploys followed within minutes).
V911 = datetime(2026, 11, 13, 19, 54, tzinfo=timezone.utc)    # unsubscribe date
V912 = datetime(2026, 11, 14, 18, 53, tzinfo=timezone.utc)    # verified date; delivery/open events
V913 = datetime(2026, 11, 14, 23, 39, tzinfo=timezone.utc)    # any signed-in visit; each email linked to its account
EMLOG_DAYS = 1095

AUTH_LABELS = {"sign_in": "Signed in", "bad_pin": "Wrong PIN entered", "locked_out": "Locked out (too many wrong PINs)",
               "pin_reset_requested": "Asked for a PIN reset link", "pin_reset": "Reset PIN with an emailed link"}


def _fmt(dt) -> str:
    from shared.usage import eastern
    d = eastern(dt)
    return f"{d:%b} {d.day}, {d.year}"


# ── The one-time step ────────────────────────────────────────────────────────

def gaps_for(u: dict, now, auth_horizon, applog_horizon) -> dict:
    """Plain-English notes for history that is missing for this account and can't come back."""
    g = {}
    created = parse(u.get("created_at"))
    old = lambda when: created is None or created < when            # noqa: E731
    if old(auth_horizon):
        g["sign_ins"] = (f"Sign-ins, wrong PINs and PIN resets before {_fmt(auth_horizon)} weren't kept "
                         f"(the sign-in log keeps 90 days).")
    if old(V913):
        g["site_visits"] = (f"Each day on the site is recorded from {_fmt(V913)}. Earlier days are known only "
                            f"when the person signed in{' after ' + _fmt(auth_horizon) if old(auth_horizon) else ''}; "
                            f"visits with a saved session weren't recorded.")
    if old(applog_horizon):
        g["emails"] = (f"Emails sent before {_fmt(applog_horizon)} aren't listed (the app log keeps 30 days)."
                       + (" Verification emails are still counted in Verify Emails." if u.get("verify_emails") else ""))
    if old(V912):
        g["email_opens"] = f"Delivery and opens are recorded from {_fmt(V912)}."
    if segments.verified(u) and not u.get("verified_at"):
        g["verified_at"] = ("Account predates email verification (March 2026), so there's no verification date."
                            if "email_verified" not in u else
                            f"Verified before {_fmt(V912)}; the date wasn't recorded.")
    if not segments.alerts_on(u) and not (u.get("alerts_off_at") or u.get("unsubscribed_at") or u.get("complained_at")):
        g["alerts_off"] = f"Alerts were turned off before {_fmt(now)}; when and how weren't recorded."
    return g


def migrate(now=None) -> dict:
    """Preserve each account's history once. Idempotent: accounts with history_since are skipped."""
    now = now or datetime.now(timezone.utc)
    users = [u for u in _to_py(_scan_all(users_table)) if u.get("email") and u["email"] != "__meta__"]
    todo = [u for u in users if not u.get("history_since")]
    if not todo:
        return {"accounts": len(users), "migrated": 0, "with_gaps": 0, "emails_copied": 0}

    auth = defaultdict(list)
    auth_rows = _to_py(_scan_all(auth_logs_tbl))
    for a in auth_rows:
        auth[(a.get("email") or "").lower()].append(a)
    stamps = [parse(a.get("ts")) for a in auth_rows if parse(a.get("ts"))]
    auth_horizon = min(stamps + [now - timedelta(days=90)])
    app_rows = [r for r in _to_py(_scan_all(app_logs_tbl)) if r.get("source") == "email" and "ok" in r]
    app_stamps = [parse(r.get("ts")) for r in app_rows if parse(r.get("ts"))]
    applog_horizon = min(app_stamps + [now - timedelta(days=30)])
    emails = defaultdict(list)
    for r in app_rows:
        who = str(r.get("user") or r.get("to") or "").lower()
        if who and who != "(admin)":
            emails[who].append(r)

    migrated = with_gaps = copied = 0
    for u in todo:
        e = u["email"].lower()
        # sign-in history: the 90-day log, kept on the account from now on
        hist = set(u.get("auth_history") or [])
        for a in auth.get(e, []):
            place = a.get("city") or a.get("region") or a.get("country") or ""
            entry = auth_history_entry(a.get("ts", ""), bool(a.get("success")), a.get("reason") or "", place)
            if entry:
                hist.add(entry)
        hist = sorted(hist)[-AUTH_HISTORY_MAX:]
        # days on the site: what's recorded plus every day with a sign-in
        days = set(u.get("site_day_list") or [])
        for entry in hist:
            ts, kind, _ = entry.split("|", 2)
            if kind == "sign_in" and parse(ts):
                days.add(day_key(parse(ts)))
        for f in ("last_login", "last_seen_at"):
            if parse(u.get(f)):
                days.add(day_key(parse(u.get(f))))
        days = sorted(days)[-MAX_DAYS:]

        sets = ["history_since = :hs", "history_gaps = :g", "auth_history = :ah", "site_day_list = :d"]
        gaps = gaps_for(u, now, auth_horizon, applog_horizon)
        vals = {":hs": now.isoformat(), ":g": gaps, ":ah": hist, ":d": days}
        if days and int(u.get("site_days") or 0) < len(days):
            sets.append("site_days = :sd"); vals[":sd"] = len(days)
        # alerts already off with a date from v9.11-9.13: record how
        if not segments.alerts_on(u) and not u.get("alerts_off_at") and (u.get("unsubscribed_at") or u.get("complained_at")):
            sets.append("alerts_off_at = :aot"); vals[":aot"] = u.get("unsubscribed_at") or u.get("complained_at")
            sets.append("alerts_off_via = :aov"); vals[":aov"] = "spam complaint" if u.get("complained_at") else "unsubscribe link"
        try:
            users_table.update_item(Key={"email": u["email"]}, UpdateExpression="SET " + ", ".join(sets),
                                    ConditionExpression="attribute_exists(email) AND attribute_not_exists(history_since)",
                                    ExpressionAttributeValues=vals)
        except Exception as ex:
            if "ConditionalCheckFailed" not in str(ex):
                print(f"[CW] timeline.migrate {e}: {ex}")
            continue
        migrated += 1
        with_gaps += 1 if gaps else 0
        # emails still in the 30-day app log from before each email was linked to its account
        for r in emails.get(e, []):
            ts = parse(r.get("ts"))
            if not ts or ts >= V913:
                continue                                   # from V913 on, em# rows carry the account
            kind = r.get("kind") or mailmeta.kind_from_subject(r.get("subject") or "")
            try:
                metrics_tbl.put_item(Item={
                    "pk": f"emlog#{r.get('log_id') or ts.isoformat()}", "account": e, "kind": kind,
                    "to": str(r.get("to") or e)[:200], "sent_at": ts.isoformat(), "ok": bool(r.get("ok")),
                    "subject": str(r.get("subject") or "")[:120], "copied_from": "app log",
                    "expires_at": int((now + timedelta(days=EMLOG_DAYS)).timestamp())},
                    ConditionExpression="attribute_not_exists(pk)")
                copied += 1
            except Exception:
                pass
    out = {"accounts": len(users), "migrated": migrated, "with_gaps": with_gaps, "emails_copied": copied,
           "auth_log_from": auth_horizon.isoformat(), "app_log_from": applog_horizon.isoformat()}
    print(f"[CW] history migration: {out}")
    return out


# ── The timeline ─────────────────────────────────────────────────────────────

def _ev(ts, kind, title, detail="", tone="", dated=True):
    return {"ts": ts.isoformat() if isinstance(ts, datetime) else (ts or ""), "kind": kind, "title": title,
            "detail": detail, "tone": tone, "dated": dated}


def build(u: dict, email_rows: list, feedback: list, auth_rows: list, now) -> dict:
    """Pure: the timeline for one account from its row and its related rows."""
    e = u["email"].lower()
    events = []
    created = parse(u.get("created_at"))
    if created:
        events.append(_ev(created, "account", "Signed up", "", "good"))

    # verification
    if parse(u.get("verified_at")):
        via = {"link": "with the emailed link", "pin_reset": "by resetting the PIN with an emailed link"}.get(
            u.get("verified_via"), "")
        n = int(u.get("verified_after_reminders") or 0)
        events.append(_ev(u["verified_at"], "account", "Verified email",
                          " ".join(x for x in (via, f"after {n} reminder{'s' if n != 1 else ''}" if n else "") if x), "good"))
    elif segments.verified(u):
        events.append(_ev(created or "", "account", "Verified email", "Date not recorded", "", dated=False))

    # sign-in history (kept on the account), or the 90-day log for accounts not yet migrated
    hist = list(u.get("auth_history") or [])
    if not u.get("history_since"):
        for a in auth_rows:
            entry = auth_history_entry(a.get("ts", ""), bool(a.get("success")), a.get("reason") or "",
                                       a.get("city") or a.get("region") or a.get("country") or "")
            if entry:
                hist.append(entry)
    signins_by_day = defaultdict(list)
    for entry in sorted(set(hist)):
        ts, kind, place = (entry.split("|", 2) + ["", ""])[:3]
        if not parse(ts):
            continue
        if kind == "sign_in":
            signins_by_day[day_key(parse(ts))].append(place)
            continue
        events.append(_ev(ts, "security", AUTH_LABELS.get(kind, kind), place,
                          "bad" if kind in ("bad_pin", "locked_out") else ""))

    # days on the site (one line per day; mentions a sign-in if there was one)
    days = set(u.get("site_day_list") or []) | set(signins_by_day)
    for d in sorted(days):
        places = [p for p in signins_by_day.get(d, []) if p]
        detail = ("Signed in" + (f" from {places[-1]}" if places else "")) if d in signins_by_day else "Signed-in visit"
        events.append(_ev(f"{d}T12:00:00+00:00", "site", "Used the site", detail, "", dated=True) | {"day_only": True})
    if parse(u.get("last_login")) and day_key(parse(u["last_login"])) not in days:
        events.append(_ev(u["last_login"], "site", "Signed in", "", ""))

    # emails
    seen_eids = set()
    for r in email_rows:
        pk = str(r.get("pk", ""))
        kind = r.get("kind") or "other"
        if kind in ("admin_report", "feedback"):
            continue
        label = mailmeta.KIND_LABELS.get(kind, "Email")
        if pk.startswith("emlog#"):
            events.append(_ev(r.get("sent_at"), "email", f"{label} {'sent' if r.get('ok', True) else 'failed to send'}",
                              "From the app log; delivery and opens weren't recorded" if parse(r.get("sent_at")) and parse(r.get("sent_at")) < V912 else "",
                              "" if r.get("ok", True) else "bad"))
            continue
        if pk in seen_eids:
            continue
        seen_eids.add(pk)
        steps = [n for f, n in (("delivered_at", "delivered"), ("opened_at", "opened"), ("clicked_at", "clicked"))
                 if r.get(f)]
        tone = ""
        if r.get("bounced_at"):
            steps.append(f"bounced ({r.get('bounce_type') or 'unknown'})"); tone = "bad"
        if r.get("complained_at"):
            steps.append("marked as spam"); tone = "bad"
        if "opened" in steps or "clicked" in steps:
            tone = tone or "good"
        when = r.get("sent_at") or r.get("first_seen") or r.get("delivered_at")
        to = r.get("to") or ""
        detail = ", ".join(steps) if steps else "sent"
        if to and to != e:
            detail += f" · to {to}"
        events.append(_ev(when, "email", label, detail, tone))
    if r_count := int(u.get("verify_emails") or 0):
        listed = sum(1 for x in events if x["kind"] == "email" and x["title"].startswith(("Verify email", "Verification reminder")))
        if r_count > listed:
            events.append(_ev(created or "", "email", f"{r_count - listed} earlier verification email{'s' if r_count - listed != 1 else ''}",
                              "Counted from server logs; dates not listed", "", dated=False))

    # alerts on / off
    off = parse(u.get("alerts_off_at")) or parse(u.get("unsubscribed_at"))
    if off:
        via = u.get("alerts_off_via") or ("spam complaint" if u.get("complained_at") else "unsubscribe link")
        events.append(_ev(off, "alerts", "Turned off weekly alerts", f"via {via}", "bad"))
    elif not segments.alerts_on(u):
        events.append(_ev(created or "", "alerts", "Turned off weekly alerts", "Date and how not recorded", "bad", dated=False))
    if parse(u.get("alerts_on_at")):
        events.append(_ev(u["alerts_on_at"], "alerts", "Turned weekly alerts back on", f"via {u.get('alerts_on_via') or 'settings'}", "good"))
    if parse(u.get("complained_at")) and not (off and u.get("alerts_off_via") == "spam complaint"):
        events.append(_ev(u["complained_at"], "alerts", "Marked an email as spam", "Unsubscribed automatically", "bad"))
    if parse(u.get("email_bounced_at")):
        events.append(_ev(u["email_bounced_at"], "alerts", "Email address bounced", "Permanent bounce reported by Resend", "bad"))

    # feedback
    for f in feedback:
        msg = " ".join(str(f.get("message") or "").split())
        events.append(_ev(f.get("ts"), "feedback", f"Sent feedback ({str(f.get('kind') or 'other').title()})",
                          (msg[:140] + "…") if len(msg) > 140 else msg, ""))

    events.sort(key=lambda x: (x["dated"], x["ts"]), reverse=True)
    p = u.get("prefs") or {}
    return {
        "email": u["email"],
        "summary": {
            "created_at": u.get("created_at", ""), "verified": segments.verified(u), "alerts_on": segments.alerts_on(u),
            "store": p.get("store_name") or (f"#{p['store_id']}" if p.get("store_id") else ""),
            "items": len(p.get("items") or []), "site_days": int(u.get("site_days") or 0),
            "alert_open_days": int(u.get("alert_open_days") or 0), "verify_emails": u.get("verify_emails"),
            "usage_30": segments.route(u, now, 30), "notify_email": p.get("notify_email") or u["email"],
        },
        "segments": segments.tags(u, now),
        "events": events,
        "history_since": u.get("history_since", ""),
        "gaps": [{"what": k, "note": v} for k, v in (u.get("history_gaps") or {}).items()],
        "retention": ["Feedback is kept for 1 year."],
    }


def admin_timeline(event, email: str):
    """GET /admin/users/{email}/timeline (wrapped with require_admin in admin.py)."""
    email = (email or "").strip().lower()
    item = users_table.get_item(Key={"email": email}).get("Item") if email else None
    if not item:
        return err("User not found.", 404)
    u = _to_py(item)
    try:
        rows = _to_py(_scan_all(metrics_tbl, FilterExpression="(begins_with(pk, :em) OR begins_with(pk, :el)) AND "
                                                              "(account = :e OR #to = :e)",
                                ExpressionAttributeNames={"#to": "to"},
                                ExpressionAttributeValues={":em": "em#", ":el": "emlog#", ":e": email}))
        fb = _to_py(_scan_all(feedback_tbl, FilterExpression="email = :e", ExpressionAttributeValues={":e": email}))
        auth = ([] if u.get("history_since") else
                _to_py(_scan_all(auth_logs_tbl, FilterExpression="email = :e", ExpressionAttributeValues={":e": email})))
        return ok(build(u, rows, fb, auth, datetime.now(timezone.utc)))
    except Exception as ex:
        print(f"[CW] admin_timeline: {ex}")
        return err("Could not build the timeline.", 500)
