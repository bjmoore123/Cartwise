"""lambda/metrics.py — the numbers behind Admin → Reports and the weekly admin email (v9.12).

    GET /admin/metrics          everything below, for the last 7 days (admin only)

build() is a pure function over rows already read from the tables, so every definition
here is unit-tested without AWS. collect() does the reading. Definitions, in one place:

  verified          email_verified is not False (accounts from before verification count as verified)
  new sign-ups      accounts created in the window. Deleted accounts vanish from this table, so a
                    week's number can shrink later; the weekly snapshot keeps what was true then.
  active            signed in within N days (users.last_login)
  setup funnel      cumulative: signed up -> verified -> store chosen -> list started -> alerts on
  email sent        rows in the app log (kept 30 days) with source=email, grouped by kind
  engagement        one row per message from Resend's events (kept 120 days); rates are over
                    unique messages: delivered / sent, opened / delivered, clicked / delivered
  value             the weekly run's own totals (run#<date> rows written by the scraper workers)

Weekly snapshots (snap#<date>, written when the Thursday report is sent, never expire) are
what make week-over-week and the 12-week trend possible: logs expire and deleted accounts
take their history with them.
"""
import os
from collections import Counter
from datetime import datetime, timezone, timedelta

from helpers import (ok, err, _to_py, _scan_all, users_table, auth_logs_tbl, app_logs_tbl,
                     scrape_logs_tbl, dynamodb)
from shared import mailmeta

metrics_tbl = dynamodb.Table(os.environ.get("METRICS_TABLE", "cartwise-metrics"))
feedback_tbl = dynamodb.Table(os.environ.get("FEEDBACK_TABLE", "cartwise-feedback"))

WINDOW_DAYS = 7
ADMIN_KINDS = ("admin_report", "feedback")          # mail to the admin, left out of user-facing totals
FUNNEL_STEPS = ("Signed up", "Verified email", "Chose a store", "Started a list", "Alerts on")
SNAP_FIELDS = ("total", "new", "verified", "unverified", "verified_rate", "active_7d", "active_30d",
               "emails_sent", "emails_failed", "unsubscribed_total", "delivered_rate", "open_rate",
               "click_rate", "bounced", "users_matched", "match_rate", "feedback_open", "errors")


def _dt(value):
    """ISO string -> aware datetime, or None. Tolerates 'Z', naive values and junk."""
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pct(part, whole, digits: int = 0):
    """Percentage, or None when there is nothing to divide by (shown as a dash, never 0%)."""
    if not whole:
        return None
    v = round(100.0 * part / whole, digits)
    return int(v) if digits == 0 else v


def _in(d, start, end) -> bool:
    return d is not None and start <= d < end


def _verified(u) -> bool:
    return u.get("email_verified") is not False


def _city(prefs: dict) -> str:
    name, sid = prefs.get("store_name") or "", prefs.get("store_id") or ""
    if name:
        parts = [x.strip() for x in name.split(",")]
        return parts[-2] if len(parts) >= 2 else (parts[-1] if parts else sid or "Unknown")
    return f"Store #{sid}" if sid else "No store set"


# ── The calculation ──────────────────────────────────────────────────────────

def build(now, users, auth_logs, app_logs, scrape_logs, feedback, metric_rows,
          deleted_total: int = 0, days: int = WINDOW_DAYS) -> dict:
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    users = [u for u in users if u.get("email") != "__meta__"]
    total = len(users)

    # ── Growth ───────────────────────────────────────────────────────────────
    created = {u["email"]: _dt(u.get("created_at")) for u in users}
    new = sum(1 for d in created.values() if _in(d, start, now))
    new_prev = sum(1 for d in created.values() if _in(d, prev_start, start))
    verified_n = sum(1 for u in users if _verified(u))
    by_month = Counter(d.strftime("%Y-%m") for d in created.values() if d)
    growth = {
        "total": total, "new": new, "new_prev": new_prev, "deleted_total": int(deleted_total or 0),
        "verified": verified_n, "unverified": total - verified_n,
        "verified_rate": _pct(verified_n, total),
        "by_month": [{"month": k, "count": v} for k, v in sorted(by_month.items())][-12:],
    }

    # ── Verification funnel ──────────────────────────────────────────────────
    cohort_start = now - timedelta(days=30)
    cohort = [u for u in users if _in(created[u["email"]], cohort_start, now)]
    c = Counter()
    for u in cohort:
        if not _verified(u):
            c["still_unverified"] += 1
            continue
        vat, cat = _dt(u.get("verified_at")), created[u["email"]]
        if not vat:
            c["verified_time_unknown"] += 1
        elif vat - cat <= timedelta(hours=24):
            c["within_24h"] += 1
        elif vat - cat <= timedelta(days=7):
            c["within_7d"] += 1
        else:
            c["later"] += 1
    by_reminder = Counter()
    for u in users:
        if _verified(u) and u.get("verified_at"):
            by_reminder[min(4, int(u.get("verified_after_reminders") or 0))] += 1
    unverified = [u for u in users if not _verified(u)]
    verification = {
        "cohort_days": 30, "cohort": len(cohort),
        "within_24h": c["within_24h"], "within_7d": c["within_7d"], "later": c["later"],
        "verified_time_unknown": c["verified_time_unknown"], "still_unverified": c["still_unverified"],
        "within_24h_rate": _pct(c["within_24h"], len(cohort)),
        "within_7d_rate": _pct(c["within_24h"] + c["within_7d"], len(cohort)),
        "by_reminder": [{"after": n, "count": by_reminder.get(n, 0)} for n in range(5)],
        "tracked": sum(by_reminder.values()),
        "unverified_over_28d": sum(1 for u in unverified
                                   if created[u["email"]] and created[u["email"]] < now - timedelta(days=28)),
        "reminders_pending": [{"sent": n, "count": sum(1 for u in unverified
                                                       if min(4, int(u.get("verify_reminders") or 0)) == n)}
                              for n in range(5)],
        "unverified_unsubscribed": sum(1 for u in unverified
                                       if (u.get("prefs") or {}).get("email_enabled") is False),
    }

    # ── Setup funnel (cumulative) ────────────────────────────────────────────
    steps = [0, 0, 0, 0, 0]
    for u in users:
        p = u.get("prefs") or {}
        checks = (True, _verified(u), bool(p.get("store_id")), bool(p.get("items")),
                  p.get("email_enabled", True) is not False)
        for i, passed in enumerate(checks):
            if not passed:
                break
            steps[i] += 1
    funnel = [{"step": FUNNEL_STEPS[i], "count": steps[i], "pct": _pct(steps[i], total),
               "lost": (steps[i - 1] - steps[i]) if i else 0} for i in range(5)]

    # ── Activity ─────────────────────────────────────────────────────────────
    last_login = {u["email"]: _dt(u.get("last_login")) for u in users}
    def _active(n):
        return sum(1 for d in last_login.values() if d and d >= now - timedelta(days=n))
    week_auth = [a for a in auth_logs if _in(_dt(a.get("ts")), start, now)]
    reasons = Counter((a.get("reason") or "") for a in week_auth)
    activity = {
        "active_7d": _active(7), "active_30d": _active(30),
        "active_7d_rate": _pct(_active(7), total), "active_30d_rate": _pct(_active(30), total),
        "dormant_60d": sum(1 for e, d in last_login.items()
                           if (d and d < now - timedelta(days=60))
                           or (not d and created[e] and created[e] < now - timedelta(days=60))),
        "never_signed_in": sum(1 for d in last_login.values() if not d),
        "sign_ins": sum(1 for a in week_auth if a.get("success") and not a.get("reason")),
        "failed_sign_ins": reasons["bad_pin"], "lockouts": reasons["locked_out"],
        "pin_resets_requested": reasons["pin_reset_requested"], "pin_resets_done": reasons["pin_reset"],
    }

    # ── Email sent (app log) ─────────────────────────────────────────────────
    sent, failed = Counter(), Counter()
    errors_by_source, errors_prev = Counter(), 0
    for row in app_logs:
        ts = _dt(row.get("ts"))
        if row.get("level") == "error":
            if _in(ts, start, now):
                errors_by_source[row.get("source") or "other"] += 1
            elif _in(ts, prev_start, start):
                errors_prev += 1
        if row.get("source") != "email" or "ok" not in row or row.get("action") or not _in(ts, start, now):
            continue
        kind = row.get("kind") or mailmeta.kind_from_subject(row.get("subject") or "")
        if kind in ADMIN_KINDS:
            continue
        (sent if row.get("ok") else failed)[kind] += 1
    kinds = [k for k in mailmeta.KINDS + ("other",) if sent.get(k) or failed.get(k)]
    unsubscribed_total = sum(1 for u in users if (u.get("prefs") or {}).get("email_enabled") is False)
    email = {
        "by_kind": [{"kind": k, "label": mailmeta.KIND_LABELS.get(k, k), "sent": sent.get(k, 0),
                     "failed": failed.get(k, 0)} for k in kinds],
        "sent": sum(sent.values()), "failed": sum(failed.values()),
        "unsubscribes": sum(1 for u in users if _in(_dt(u.get("unsubscribed_at")), start, now)),
        "unsubscribed_total": unsubscribed_total,
        "unsubscribed_rate": _pct(unsubscribed_total, total),
        "spam_complaints": sum(1 for u in users if _in(_dt(u.get("complained_at")), start, now)),
        "bounced_accounts": sum(1 for u in users if u.get("email_bounced_at")),
    }

    # ── Email engagement (Resend events) ─────────────────────────────────────
    eng_kind, problems = {}, []
    for row in metric_rows:
        if not str(row.get("pk", "")).startswith("em#"):
            continue
        first = _dt(row.get("sent_at")) or _dt(row.get("first_seen")) or _dt(row.get("delivered_at"))
        kind = row.get("kind") or "other"
        if not _in(first, start, now) or kind in ADMIN_KINDS:
            continue
        k = eng_kind.setdefault(kind, Counter())
        k["sent"] += 1
        for field, name in (("delivered_at", "delivered"), ("opened_at", "opened"), ("clicked_at", "clicked"),
                            ("bounced_at", "bounced"), ("complained_at", "complained")):
            if row.get(field):
                k[name] += 1
        if row.get("bounced_at") or row.get("complained_at"):
            problems.append({"to": row.get("to", ""), "kind": mailmeta.KIND_LABELS.get(kind, kind),
                             "what": "Marked as spam" if row.get("complained_at")
                                     else f"Bounced ({row.get('bounce_type') or 'unknown'})",
                             "when": row.get("complained_at") or row.get("bounced_at")})
    tot = Counter()
    for k in eng_kind.values():
        tot.update(k)
    def _eng(k):
        return {"sent": k["sent"], "delivered": k["delivered"], "opened": k["opened"], "clicked": k["clicked"],
                "bounced": k["bounced"], "complained": k["complained"],
                "delivered_rate": _pct(k["delivered"], k["sent"]), "open_rate": _pct(k["opened"], k["delivered"]),
                "click_rate": _pct(k["clicked"], k["delivered"]), "bounce_rate": _pct(k["bounced"], k["sent"], 1)}
    engagement = {
        "has_data": tot["sent"] > 0, **_eng(tot),
        "has_opens": tot["opened"] > 0, "has_clicks": tot["clicked"] > 0,
        "by_kind": [{"kind": k, "label": mailmeta.KIND_LABELS.get(k, k), **_eng(v)}
                    for k, v in sorted(eng_kind.items(), key=lambda kv: -kv[1]["sent"])],
        "problems": sorted(problems, key=lambda p: p["when"] or "", reverse=True)[:20],
    }

    # ── Value delivered (weekly run totals) ──────────────────────────────────
    runs = sorted((r for r in metric_rows if str(r.get("pk", "")).startswith("run#")),
                  key=lambda r: r["pk"], reverse=True)
    def _run(r):
        with_list, matched = int(r.get("users_with_list") or 0), int(r.get("users_matched") or 0)
        return {"date": r["pk"][4:], "stores": int(r.get("stores") or 0),
                "users_with_list": with_list, "users_matched": matched,
                "zero_match": max(0, with_list - matched), "match_rate": _pct(matched, with_list),
                "matches_total": int(r.get("matches_total") or 0),
                "avg_matches": round(int(r.get("matches_total") or 0) / matched, 1) if matched else None,
                "emails_sent": int(r.get("emails_sent") or 0),
                "skipped_unverified": int(r.get("skipped_unverified") or 0),
                "skipped_opted_out": int(r.get("skipped_opted_out") or 0),
                "send_failed": int(r.get("send_failed") or 0)}
    value = {"has_data": bool(runs), "last": _run(runs[0]) if runs else None,
             "previous": _run(runs[1]) if len(runs) > 1 else None, "top_items": []}
    if runs:
        items_row = next((r for r in metric_rows if r.get("pk") == "runitems#" + runs[0]["pk"][4:]), {})
        top = sorted(((k[2:], int(v)) for k, v in items_row.items() if str(k).startswith("i:")),
                     key=lambda kv: (-kv[1], kv[0]))[:10]
        value["top_items"] = [{"item": k, "count": v} for k, v in top]

    # ── Lists and stores ─────────────────────────────────────────────────────
    all_items, by_city, by_store, sizes = Counter(), Counter(), Counter(), []
    for u in users:
        p = u.get("prefs") or {}
        by_city[_city(p)] += 1
        if p.get("store_id"):
            by_store[str(p["store_id"])] += 1
        its = [i.strip().lower() for i in (p.get("items") or []) if isinstance(i, str) and i.strip()]
        sizes.append(len(its))
        all_items.update(its)
    with_list = [s for s in sizes if s]
    lists = {
        "avg_items": round(sum(sizes) / total, 1) if total else 0,
        "avg_items_nonempty": round(sum(with_list) / len(with_list), 1) if with_list else 0,
        "with_list": len(with_list), "empty_lists": total - len(with_list),
        "stores": len(by_store), "single_user_stores": sum(1 for v in by_store.values() if v == 1),
        "no_store": total - sum(by_store.values()),
        "popular_items": [{"item": k, "count": v} for k, v in all_items.most_common(25)],
        "by_geography": [{"location": k, "count": v} for k, v in by_city.most_common(20)],
    }

    # ── Site health ──────────────────────────────────────────────────────────
    week_scrapes = sorted((s for s in scrape_logs if _in(_dt(s.get("started_at")), start, now)),
                          key=lambda s: s.get("started_at", ""), reverse=True)
    all_scrapes = sorted(scrape_logs, key=lambda s: s.get("started_at", ""), reverse=True)
    def _scrape(s):
        return {"started_at": s.get("started_at", ""), "stores": int(s.get("stores_examined") or 0),
                "deals": int(s.get("total_deals") or 0), "emails_sent": int(s.get("emails_sent") or 0),
                "errors": len(s.get("errors") or [])}
    week_fb = [f for f in feedback if _in(_dt(f.get("ts")), start, now)]
    health = {
        "scrape_runs": len(week_scrapes),
        "scrape_runs_with_errors": sum(1 for s in week_scrapes if s.get("errors")),
        "last_scrape": _scrape(all_scrapes[0]) if all_scrapes else None,
        "errors": sum(errors_by_source.values()), "errors_prev": errors_prev,
        "errors_by_source": [{"source": k, "count": v} for k, v in errors_by_source.most_common()],
        "feedback_new": len(week_fb), "feedback_open": sum(1 for f in feedback if f.get("status") == "new"),
    }

    # ── Trend and week-over-week (snapshots) ─────────────────────────────────
    snaps = sorted((r for r in metric_rows if str(r.get("pk", "")).startswith("snap#")),
                   key=lambda r: r["pk"])
    trend = [{"week": r["pk"][5:], **{f: r.get(f) for f in SNAP_FIELDS}} for r in snaps][-12:]
    # "last week" = the newest snapshot at least 5 days old (so re-running today compares to last week)
    cutoff = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    older = [t for t in trend if t["week"] <= cutoff]
    previous = older[-1] if older else None

    headline = {
        "total": total, "new": new, "verified": verified_n, "unverified": total - verified_n,
        "verified_rate": growth["verified_rate"],
        "active_7d": activity["active_7d"], "active_30d": activity["active_30d"],
        "emails_sent": email["sent"], "emails_failed": email["failed"],
        "unsubscribed_total": unsubscribed_total,
        "delivered_rate": engagement["delivered_rate"], "open_rate": engagement["open_rate"],
        "click_rate": engagement["click_rate"], "bounced": engagement["bounced"],
        "users_matched": value["last"]["users_matched"] if value["last"] else None,
        "match_rate": value["last"]["match_rate"] if value["last"] else None,
        "feedback_open": health["feedback_open"], "errors": health["errors"],
    }

    out = {
        "generated_at": now.isoformat(),
        "window": {"days": days, "start": start.isoformat(), "end": now.isoformat()},
        "env": mailmeta.env_name(),
        "headline": headline, "previous": previous,
        "growth": growth, "verification": verification, "funnel": funnel, "activity": activity,
        "email": email, "engagement": engagement, "value": value, "lists": lists, "health": health,
        "trend": trend,
    }
    out["attention"] = _attention(out, now)
    return out


def _attention(m: dict, now) -> list:
    """Only the things worth acting on this week. Empty list = nothing to do."""
    out = []
    def add(level, text):
        out.append({"level": level, "text": text})
    h, e, g, v = m["health"], m["email"], m["engagement"], m["value"]
    if h["scrape_runs_with_errors"]:
        add("bad", f'{h["scrape_runs_with_errors"]} of {h["scrape_runs"]} deal-fetch runs had errors '
                   f'(Admin → Logging → scrape log).')
    if not h["scrape_runs"]:
        add("bad", "No deal-fetch run in the last 7 days. The Wednesday job may not be running.")
    if e["failed"]:
        add("bad", f'{e["failed"]} email{"s" if e["failed"] != 1 else ""} failed to send.')
    elif v["last"] and v["last"]["send_failed"]:
        add("bad", f'{v["last"]["send_failed"]} weekly alert(s) failed to send in the last run.')
    if e["spam_complaints"] or g["complained"]:
        n = max(e["spam_complaints"], g["complained"])
        add("bad", f'{n} spam complaint{"s" if n != 1 else ""}. Those accounts were unsubscribed automatically.')
    if g["bounced"]:
        add("warn", f'{g["bounced"]} email{"s" if g["bounced"] != 1 else ""} bounced '
                    f'({g["bounce_rate"]}% of sent). The addresses are listed in Admin → Reports.')
    if h["errors"] >= 10 and h["errors"] > 2 * max(1, h["errors_prev"]):
        add("warn", f'Errors jumped to {h["errors"]} this week from {h["errors_prev"]} last week.')
    if h["feedback_open"]:
        add("warn", f'{h["feedback_open"]} feedback message{"s" if h["feedback_open"] != 1 else ""} '
                    f'waiting in Admin → Feedback.')
    if m["verification"]["unverified_over_28d"]:
        n = m["verification"]["unverified_over_28d"]
        add("info", f'{n} account{"s" if n != 1 else ""} still unverified after 28 days and all reminders.')
    if not g["has_data"] and e["sent"]:
        add("info", "No delivery or open data yet. Turn on the email events in Resend (see the v9.12 test plan).")
    return out


# ── Reading the tables ───────────────────────────────────────────────────────

def collect(now=None, days: int = WINDOW_DAYS) -> dict:
    now = now or datetime.now(timezone.utc)
    users = _to_py(_scan_all(users_table))
    meta = next((u for u in users if u.get("email") == "__meta__"), {})
    return build(
        now, users,
        auth_logs=_to_py(_scan_all(auth_logs_tbl, ProjectionExpression="ts, success, reason")),
        app_logs=_to_py(_scan_all(app_logs_tbl, ProjectionExpression="ts, #s, #l, ok, kind, subject, #a",
                                  ExpressionAttributeNames={"#s": "source", "#l": "level", "#a": "action"})),
        scrape_logs=_to_py(_scan_all(scrape_logs_tbl)),
        feedback=_to_py(_scan_all(feedback_tbl, ProjectionExpression="ts, #s",
                                  ExpressionAttributeNames={"#s": "status"})),
        metric_rows=_to_py(_scan_all(metrics_tbl)),
        deleted_total=int(meta.get("deleted_count") or 0), days=days)


def save_snapshot(m: dict, now=None) -> str:
    """Keep this week's headline numbers forever (one small row per report date)."""
    now = now or datetime.now(timezone.utc)
    key = f"snap#{now:%Y-%m-%d}"
    item = {"pk": key, "saved_at": now.isoformat()}
    for f in SNAP_FIELDS:
        val = m["headline"].get(f)
        if val is not None:
            item[f] = int(val) if float(val).is_integer() else str(val)
    metrics_tbl.put_item(Item=item)
    return key


def admin_metrics(event):
    """GET /admin/metrics (wrapped with require_admin in admin.py)."""
    try:
        return ok(collect())
    except Exception as e:
        print(f"[CW] admin_metrics: {e}")
        return err("Could not build the report.", 500)
