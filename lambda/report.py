"""lambda/report.py — the weekly admin report email (v9.12).

Sent every Thursday morning (EventBridge rule <app>-admin-report -> this Lambda with
{"source": "admin-weekly-report"}), the day after the Wednesday deal emails, so each report
covers one complete weekly cycle. Also: POST /admin/report/send (admin only) sends one now.

  - Goes only to the admin address in SSM (/publix/feedback-to -> FEEDBACK_TO). The address
    is never sent to the browser or written to logs.
  - The scheduled run saves the week's snapshot (metrics.save_snapshot) AFTER building the
    report, so "vs last week" always compares against the previous Thursday.
  - Same numbers as Admin -> Reports: both come from metrics.collect().
  - Admin mail, so no unsubscribe footer (it isn't sent to users).
"""
import os
from datetime import datetime, timezone, timedelta
from html import escape

from helpers import ok, err
from logging_utils import _log_app_event
from shared import email_layout as L
from shared import mailmeta
import metrics

FRONTEND_URL = os.environ.get("FRONTEND_URL", "")
RED, AMBER_TXT = "#b3261e", "#8a4106"


# ── Small formatting helpers ─────────────────────────────────────────────────

def _nth_sunday(year: int, month: int, n: int) -> int:
    """Day of the month of the n-th Sunday."""
    first = datetime(year, month, 1).weekday()            # Monday = 0 ... Sunday = 6
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def _local(dt):
    """UTC -> US Eastern, without the time zone database (it isn't installed on Windows, and
    isn't guaranteed on Lambda). Daylight time runs from 2 AM on the second Sunday of March
    to 2 AM on the first Sunday of November, i.e. 07:00 UTC to 06:00 UTC on those days."""
    u = dt.astimezone(timezone.utc)
    start = datetime(u.year, 3, _nth_sunday(u.year, 3, 2), 7, tzinfo=timezone.utc)
    end = datetime(u.year, 11, _nth_sunday(u.year, 11, 1), 6, tzinfo=timezone.utc)
    return u - timedelta(hours=4 if start <= u < end else 5)


def _day(iso: str) -> str:
    """'Nov 18'. A plain date (2026-11-18) is shown as written; a timestamp is shown in Eastern time."""
    if isinstance(iso, str) and len(iso) == 10:
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
            return f"{d:%b} {d.day}"
        except ValueError:
            pass
    d = _local(metrics._dt(iso) or datetime.now(timezone.utc))
    return f"{d:%b} {d.day}"


def _num(v, suffix: str = "") -> str:
    return "–" if v is None else f"{v:,}{suffix}" if isinstance(v, int) else f"{v}{suffix}"


def delta(cur, prev, unit: str = "", noun: str = "vs last week") -> str:
    """'+3 vs last week', '-2 pts vs last week', 'same as last week', or '' with nothing to compare."""
    if cur is None or prev is None:
        return ""
    try:
        d = int(cur) - int(prev)
    except (TypeError, ValueError):
        return ""
    if d == 0:
        return "same as last week"
    return f"{'+' if d > 0 else '−'}{abs(d):,}{unit} {noun}"


def _tile(label: str, value: str, note: str, left: bool = True) -> str:
    pad = "0 6px 12px 0" if left else "0 0 12px 6px"
    return (f'<td width="50%" style="padding:{pad}" valign="top">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td style="background:{L.MINT};border-radius:10px;padding:12px 14px;font-family:{L.SANS}">'
            f'<div style="font-size:11px;line-height:16px;font-weight:700;letter-spacing:.06em;'
            f'text-transform:uppercase;color:{L.MUTED}">{escape(label)}</div>'
            f'<div style="font-family:{L.SERIF};font-size:30px;line-height:36px;color:{L.GREEN}">{escape(value)}</div>'
            f'<div style="font-size:12px;line-height:18px;color:{L.MUTED}">{escape(note) or "&nbsp;"}</div>'
            f'</td></tr></table></td>')


def _h2(text: str) -> str:
    return (f'<h2 style="margin:26px 0 10px;font-family:{L.SERIF};font-size:19px;line-height:26px;'
            f'font-weight:400;color:{L.INK}">{escape(text)}</h2>')


def _rows(pairs, widths=("62%", "38%")) -> str:
    """Label / value rows. Values are right-aligned and never wrap."""
    out = []
    for i, (label, value) in enumerate(pairs):
        line = f"border-top:1px solid {L.LINE};" if i else ""
        out.append(f'<tr><td width="{widths[0]}" style="{line}padding:7px 0;font-family:{L.SANS};font-size:14px;'
                   f'line-height:20px;color:{L.BODY}">{label}</td>'
                   f'<td width="{widths[1]}" align="right" style="{line}padding:7px 0;font-family:{L.SANS};'
                   f'font-size:14px;line-height:20px;font-weight:700;color:{L.INK};white-space:nowrap">{value}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'{"".join(out)}</table>')


def _grid(head, rows) -> str:
    """A small numeric table: first column text, the rest right-aligned numbers."""
    th = "".join(f'<td align="{"left" if i == 0 else "right"}" style="padding:6px 0 6px {0 if i == 0 else 6}px;'
                 f'font-family:{L.SANS};font-size:10px;line-height:16px;font-weight:700;letter-spacing:.03em;'
                 f'text-transform:uppercase;color:{L.MUTED};white-space:nowrap">{escape(h)}</td>'
                 for i, h in enumerate(head))
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td align="{"left" if i == 0 else "right"}" style="border-top:1px solid {L.LINE};'
            f'padding:7px 0 7px {0 if i == 0 else 6}px;font-family:{L.SANS};font-size:14px;line-height:20px;'
            f'color:{L.BODY if i == 0 else L.INK};{"" if i == 0 else "font-weight:700;white-space:nowrap"}">{c}</td>'
            for i, c in enumerate(r)) + "</tr>"
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr>{th}</tr>{body}</table>')


def _funnel(steps) -> str:
    top = max(1, steps[0]["count"])
    out = ""
    for i, s in enumerate(steps):
        w = max(2, round(100 * s["count"] / top))
        lost = (f' <span style="color:{L.MUTED};font-weight:400">(−{s["lost"]})</span>' if s["lost"] else "")
        out += (f'<tr><td style="padding:{0 if i == 0 else 8}px 0 0;font-family:{L.SANS};font-size:14px;line-height:20px;'
                f'color:{L.BODY}">{escape(s["step"])}</td>'
                f'<td align="right" style="padding:{0 if i == 0 else 8}px 0 0;font-family:{L.SANS};font-size:14px;'
                f'line-height:20px;font-weight:700;color:{L.INK};white-space:nowrap">'
                f'{s["count"]:,} · {_num(s["pct"], "%")}{lost}</td></tr>'
                f'<tr><td colspan="2" style="padding:3px 0 0">'
                f'<table role="presentation" width="{w}%" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td height="8" style="height:8px;line-height:8px;font-size:0;background:{L.GREEN};'
                f'border-radius:4px">&nbsp;</td></tr></table></td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{out}</table>'


def _attention(items) -> str:
    if not items:
        return L.callout("<strong>Nothing needs attention this week.</strong>")
    color = {"bad": RED, "warn": AMBER_TXT, "info": L.BODY}
    lis = "".join(f'<tr><td valign="top" style="padding:3px 8px 3px 0;font-size:14px;line-height:20px;'
                  f'color:{color.get(i["level"], L.BODY)}">●</td>'
                  f'<td style="padding:3px 0;font-family:{L.SANS};font-size:14px;line-height:20px;'
                  f'color:{L.BODY}">{escape(i["text"])}</td></tr>' for i in items)
    bad = any(i["level"] == "bad" for i in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 4px">'
            f'<tr><td style="background:{"#fdf3f2" if bad else "#fff8e6"};border-left:3px solid '
            f'{RED if bad else L.AMBER};border-radius:6px;padding:12px 16px">'
            f'<div style="font-family:{L.SANS};font-size:12px;line-height:18px;font-weight:700;letter-spacing:.05em;'
            f'text-transform:uppercase;color:{L.INK};padding-bottom:4px">Needs attention</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{lis}</table></td></tr></table>')


# ── The email ────────────────────────────────────────────────────────────────

def subject_line(m: dict) -> str:
    h = m["headline"]
    tag = "[staging] " if m.get("env") == "staging" else ""
    return (f'{tag}Cartwise weekly report: {h["total"]:,} users, {h["new"]} new, '
            f'{_num(h["verified_rate"], "%")} verified')


def build_html(m: dict, site: str = "") -> str:
    h, prev = m["headline"], (m.get("previous") or {})
    g, ver, act, em, eng, val, hl, ls = (m["growth"], m["verification"], m["activity"], m["email"],
                                         m["engagement"], m["value"], m["health"], m["lists"])
    period = f'{_day(m["window"]["start"])} – {_day(m["window"]["end"])}'
    first = "" if prev else "first report"

    tiles = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
             + _tile("Total users", _num(h["total"]), delta(h["total"], prev.get("total")) or first)
             + _tile("New sign-ups", _num(h["new"]),
                     delta(h["new"], g["new_prev"]) if (h["new"] or g["new_prev"]) else "none last week either",
                     left=False)
             + "</tr><tr>"
             + _tile("Verified", _num(h["verified_rate"], "%"),
                     delta(h["verified_rate"], prev.get("verified_rate"), " pts")
                     or f'{h["verified"]:,} of {h["total"]:,}')
             + _tile("Active this week", _num(h["active_7d"]),
                     delta(h["active_7d"], prev.get("active_7d")) or f'{_num(act["active_7d_rate"], "%")} of users',
                     left=False)
             + "</tr></table>")

    verify_rows = [
        (f'Sign-ups in the last {ver["cohort_days"]} days', _num(ver["cohort"])),
        ("Verified within 24 hours", f'{ver["within_24h"]} · {_num(ver["within_24h_rate"], "%")}'),
        ("Verified within 7 days", f'{ver["within_24h"] + ver["within_7d"]} · {_num(ver["within_7d_rate"], "%")}'),
        ("Still unverified", _num(ver["still_unverified"])),
        ("Unverified more than 28 days (all accounts)", _num(ver["unverified_over_28d"])),
    ]
    if ver["tracked"]:
        after = ", ".join(f'reminder {r["after"]}: {r["count"]}' for r in ver["by_reminder"] if r["after"] and r["count"])
        verify_rows.append(("Verified with no reminder / after a reminder",
                            f'{ver["by_reminder"][0]["count"]} / {ver["tracked"] - ver["by_reminder"][0]["count"]}'))
        if after:
            verify_rows.append((f'<span style="color:{L.MUTED}">Verified after {escape(after)}</span>', ""))

    if em["by_kind"]:
        by_eng = {k["kind"]: k for k in eng["by_kind"]}
        if eng["has_data"]:
            head = ["Email", "Sent", "Failed", "Deliv.", "Opened"]
            rows = [[escape(k["label"]), _num(k["sent"]), _num(k["failed"]),
                     _num(by_eng.get(k["kind"], {}).get("delivered_rate"), "%"),
                     _num(by_eng.get(k["kind"], {}).get("open_rate"), "%") if eng["has_opens"] else "–"]
                    for k in em["by_kind"]]
            rows.append(["<strong>All</strong>", _num(em["sent"]), _num(em["failed"]),
                         _num(eng["delivered_rate"], "%"), _num(eng["open_rate"], "%") if eng["has_opens"] else "–"])
        else:
            head = ["Email", "Sent", "Failed"]
            rows = [[escape(k["label"]), _num(k["sent"]), _num(k["failed"])] for k in em["by_kind"]]
            rows.append(["<strong>All</strong>", _num(em["sent"]), _num(em["failed"])])
        email_html = _grid(head, rows)
    else:
        email_html = L.p("No emails were sent this week.", small=True, muted=True)
    email_html += _rows([
        ("Unsubscribed this week", _num(em["unsubscribes"])),
        ("Unsubscribed in total", f'{em["unsubscribed_total"]} · {_num(em["unsubscribed_rate"], "%")}'),
        ("Bounced / marked as spam this week", f'{eng["bounced"]} / {max(eng["complained"], em["spam_complaints"])}'),
    ])

    if val["last"]:
        r, p = val["last"], (val["previous"] or {})
        deals = _rows([
            (f'Users with a list checked ({escape(_day(r["date"]))})', _num(r["users_with_list"])),
            ("Users with at least one match"
             + (f' <span style="color:{L.MUTED}">({escape(delta(r["users_matched"], p.get("users_matched")))})</span>'
                if p else ""), f'{r["users_matched"]} · {_num(r["match_rate"], "%")}'),
            ("Users with a list but no match", _num(r["zero_match"])),
            ("Matches found / average per matched user", f'{r["matches_total"]:,} / {_num(r["avg_matches"])}'),
            ("Alert emails sent", _num(r["emails_sent"])),
            ("Matched but not emailed (unverified / opted out)",
             f'{r["skipped_unverified"]} / {r["skipped_opted_out"]}'),
        ])
        if val["top_items"]:
            deals += L.p("Most matched items: " + ", ".join(
                f'{escape(t["item"])} ({t["count"]})' for t in val["top_items"][:8]) + ".",
                small=True, muted=True, last=True).replace("margin:0 0 0px", "margin:10px 0 0")
    else:
        deals = L.p("No weekly run has been recorded yet. This fills in after the next Wednesday deal email.",
                    small=True, muted=True)

    last = hl["last_scrape"]
    health = _rows([
        ("Signed in during the last 30 days", f'{act["active_30d"]} · {_num(act["active_30d_rate"], "%")}'),
        ("No sign-in for 60+ days", _num(act["dormant_60d"])),
        ("Sign-ins / failed sign-ins / lockouts", f'{act["sign_ins"]} / {act["failed_sign_ins"]} / {act["lockouts"]}'),
        ("PIN resets requested / completed", f'{act["pin_resets_requested"]} / {act["pin_resets_done"]}'),
        ("Deal-fetch runs (with errors)", f'{hl["scrape_runs"]} ({hl["scrape_runs_with_errors"]})'),
        ("Last deal fetch", f'{_day(last["started_at"])} · {last["stores"]} stores' if last else "–"),
        ("Errors logged (last week)", f'{hl["errors"]} ({hl["errors_prev"]})'),
        ("Feedback received / still open", f'{hl["feedback_new"]} / {hl["feedback_open"]}'),
        ("Average list size / empty lists", f'{ls["avg_items_nonempty"]} / {ls["empty_lists"]}'),
    ])

    s = L.site_url(site)
    body = (f'<div class="cw-body" style="padding:28px 30px 12px">'
            f'<h1 class="cw-h1" style="margin:0 0 4px;font-family:{L.SERIF};font-size:26px;line-height:32px;'
            f'font-weight:400;color:{L.INK}">Weekly report</h1>'
            f'<div style="font-family:{L.SANS};font-size:14px;line-height:20px;color:{L.MUTED};margin:0 0 16px">'
            f'{escape(period)}{" · staging" if m.get("env") == "staging" else ""}</div>'
            + tiles + _attention(m["attention"])
            + _h2("Setup funnel") + _funnel(m["funnel"])
            + _h2("Verification") + _rows(verify_rows)
            + _h2("Email") + email_html
            + _h2("This week's deals") + deals
            + _h2("Activity and site health") + health
            + '<div style="padding-top:18px">' + L.button("Open Admin reports", s + "/#admin") + "</div>"
            + "</div>")
    return L.email(title="Cartwise weekly report", preheader=subject_line(m).split(": ", 1)[-1] + f" · {period}",
                   heading="", body=body, raw_body=True, wide=True, site=site,
                   reason="Admin report. Sent every Thursday morning.")


def build_text(m: dict, site: str = "") -> str:
    h, act, em, eng, val, hl, ver = (m["headline"], m["activity"], m["email"], m["engagement"],
                                     m["value"], m["health"], m["verification"])
    period = f'{_day(m["window"]["start"])} – {_day(m["window"]["end"])}'
    lines = [f"Total users: {h['total']}   New sign-ups: {h['new']} (previous week {m['growth']['new_prev']})",
             f"Verified: {_num(h['verified_rate'], '%')} ({h['verified']} of {h['total']})   "
             f"Active this week: {h['active_7d']}"]
    attention = ("Needs attention:\n" + "\n".join(f"- {a['text']}" for a in m["attention"])
                 if m["attention"] else "Nothing needs attention this week.")
    funnel = "Setup funnel:\n" + "\n".join(f"- {s['step']}: {s['count']} ({_num(s['pct'], '%')})" for s in m["funnel"])
    verify = (f"Verification (sign-ups in the last {ver['cohort_days']} days: {ver['cohort']}): "
              f"{ver['within_24h']} within 24 hours, {ver['within_24h'] + ver['within_7d']} within 7 days, "
              f"{ver['still_unverified']} still unverified.")
    email = (f"Email: {em['sent']} sent, {em['failed']} failed, {em['unsubscribes']} unsubscribed"
             + (f", {_num(eng['delivered_rate'], '%')} delivered, {_num(eng['open_rate'], '%')} opened, "
                f"{eng['bounced']} bounced" if eng["has_data"] else "") + ".")
    if val["last"]:
        r = val["last"]
        deals = (f"This week's deals ({_day(r['date'])}): {r['users_matched']} of {r['users_with_list']} users with a "
                 f"list had a match ({_num(r['match_rate'], '%')}); {r['emails_sent']} alert emails sent.")
    else:
        deals = "This week's deals: no weekly run recorded yet."
    health = (f"Health: {hl['scrape_runs']} deal-fetch runs ({hl['scrape_runs_with_errors']} with errors), "
              f"{hl['errors']} errors logged, {hl['feedback_open']} feedback open. "
              f"Sign-ins {act['sign_ins']}, failed {act['failed_sign_ins']}, lockouts {act['lockouts']}.")
    return L.text(f"Cartwise weekly report, {period}", "\n".join(lines), attention, funnel, verify, email,
                  deals, health, f"Full report:\n{L.site_url(site)}/#admin",
                  reason="Admin report. Sent every Thursday morning.", site=site)


# ── Sending ──────────────────────────────────────────────────────────────────

def send_report(scheduled: bool = False, now=None) -> dict:
    """Build the report and email it to the admin. The scheduled run also saves the snapshot."""
    to = os.environ.get("FEEDBACK_TO", "").strip()
    key = os.environ.get("RESEND_API_KEY", "")
    now = now or datetime.now(timezone.utc)
    m = metrics.collect(now)
    out = {"sent": False, "snapshot": None}
    if scheduled:
        try:
            out["snapshot"] = metrics.save_snapshot(m, now)
        except Exception as e:
            print(f"[CW] report: snapshot not saved: {e}")
    if not to or not key:
        print("[CW] report: FEEDBACK_TO or RESEND_API_KEY not set; report not emailed")
        out["reason"] = "not_configured"
        return out
    sender = (f"{os.environ.get('RESEND_FROM_NAME', 'Cartwise Alerts')} "
              f"<{os.environ.get('RESEND_FROM_ADDR', 'onboarding@resend.dev')}>")
    subject = subject_line(m)
    try:
        import resend as _resend
        _resend.api_key = key
        _resend.Emails.send({"from": sender, "to": [to], "subject": subject,
                             "html": build_html(m, FRONTEND_URL), "text": build_text(m, FRONTEND_URL),
                             "tags": mailmeta.tags("admin_report")})
        out["sent"] = True
        _log_app_event("email", "info", ok=True, to="(admin)", kind="admin_report", subject=subject[:120],
                       trigger="scheduled" if scheduled else "manual")
    except Exception as e:
        print(f"[CW] report: send failed: {e}")
        out["reason"] = "send_failed"
        _log_app_event("email", "error", ok=False, to="(admin)", kind="admin_report", subject="weekly report",
                       message=str(e)[:200])
    return out


def admin_send_report(event, body=None):
    """POST /admin/report/send (wrapped with require_admin in admin.py)."""
    try:
        res = send_report(scheduled=False)
    except Exception as e:
        print(f"[CW] admin_send_report: {e}")
        return err("Could not build the report.", 500)
    if res["sent"]:
        return ok({"message": "Report sent to the admin address."})
    if res.get("reason") == "not_configured":
        return err("No admin address is configured (/publix/feedback-to), so the report can't be emailed.", 400)
    return err("The report could not be sent. Check Admin → Logging.", 502)
