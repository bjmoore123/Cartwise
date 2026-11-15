"""lambda/segments.py — groups of users, defined once (v9.14).

Every clickable number on Admin → Reports is the size of one of these groups, and clicking
it lists exactly the accounts tagged with that group on Admin → Users. Both come from
tags() below, so a count and its list can't disagree.

Pure functions over one account row and `now`; no AWS.
"""
from datetime import datetime, timedelta

from shared.usage import parse, day_key

# key: (group, label, help, extra column for the Users list: (header, field) or None)
CATALOG = {
    # How people use Cartwise
    "use_both_7":   ("Usage", "Email and site, last 7 days", "Opened a weekly alert and used the site", None),
    "use_email_7":  ("Usage", "Email only, last 7 days", "Opened or clicked a weekly alert; no site use", ("Last alert opened", "last_alert_open_at")),
    "use_site_7":   ("Usage", "Site only, last 7 days", "Used the site; no alert opened", ("Last on the site", "site_last")),
    "use_none_7":   ("Usage", "Not seen, last 7 days", "No site use and no alert opened", None),
    "engaged_7":    ("Usage", "Engaged, last 7 days", "Opened a weekly alert or used the site", None),
    "use_both_30":  ("Usage", "Email and site, last 30 days", "Opened a weekly alert and used the site", None),
    "use_email_30": ("Usage", "Email only, last 30 days", "Opened or clicked a weekly alert; no site use", ("Last alert opened", "last_alert_open_at")),
    "use_site_30":  ("Usage", "Site only, last 30 days", "Used the site; no alert opened", ("Last on the site", "site_last")),
    "use_none_30":  ("Usage", "Not seen, last 30 days", "No site use and no alert opened", None),
    "engaged_30":   ("Usage", "Engaged, last 30 days", "Opened a weekly alert or used the site", None),
    "recipient_unengaged_30": ("Usage", "Alert recipients not seen in 30 days",
                               "Getting weekly alerts but no open and no site use", None),
    "dormant_60":   ("Usage", "Not seen for 60+ days", "Account older than 60 days; no site use or alert opened in 60 days", None),
    # Email
    "alerts_off":   ("Email", "Alerts turned off", "Unsubscribed, turned off in Notifications, by an admin, or after a spam complaint",
                     ("Turned off", "alerts_off")),
    "unsub_7":      ("Email", "Unsubscribed in the last 7 days", "Alerts turned off in the last 7 days", ("Turned off", "alerts_off")),
    "spam":         ("Email", "Marked an email as spam", "Unsubscribed automatically", ("Marked as spam", "complained_at")),
    "spam_7":       ("Email", "Marked as spam in the last 7 days", "Unsubscribed automatically", ("Marked as spam", "complained_at")),
    "bounced":      ("Email", "Address bounced", "Resend reported a permanent bounce", ("Bounced", "email_bounced_at")),
    "alert_recipient": ("Email", "Getting weekly alerts", "Verified, alerts on, store and list set", None),
    # Verification
    "unverified":           ("Verification", "Unverified", "Email address not verified", ("Verify emails", "verify_emails")),
    "unverified_new_30":    ("Verification", "Signed up in 30 days, still unverified", "", ("Verify emails", "verify_emails")),
    "unverified_reminding": ("Verification", "Unverified, reminders still going", "One reminder a week, 4 in all", ("Reminders sent", "verify_reminders")),
    "unverified_finished":  ("Verification", "Unverified after all 4 reminders", "The last link has expired", ("Last reminder", "last_verify_reminder_at")),
    "unverified_no_email":  ("Verification", "Unverified, no verification email on record", "", ("Joined", "created_at")),
    "unverified_unsub":     ("Verification", "Unverified and unsubscribed", "", ("Turned off", "alerts_off")),
    # Setup (each funnel step's drop-off)
    "new_7":        ("Setup", "Signed up in the last 7 days", "", ("Joined", "created_at")),
    "stop_verify":  ("Setup", "Stopped before verifying", "Signed up, not verified", ("Joined", "created_at")),
    "stop_store":   ("Setup", "Stopped before choosing a store", "Verified, no store", ("Joined", "created_at")),
    "stop_list":    ("Setup", "Stopped before starting a list", "Store chosen, no items", ("Joined", "created_at")),
    "stop_alerts":  ("Setup", "List started, alerts off", "Store and items set, alerts turned off", ("Turned off", "alerts_off")),
    "no_store":     ("Setup", "No store", "", None),
    "empty_list":   ("Setup", "Empty list", "", None),
    "history_gaps": ("Data", "History incomplete", "Some history before tracking started was not recorded", ("Missing", "history_gaps")),
    # v9.14.2: the rest of the Reports numbers that count people
    "verified":           ("Verification", "Verified", "Email address verified", None),
    "verified_24h_30":    ("Verification", "Signed up in 30 days, verified within 24 hours", "", ("Verified", "verified_at")),
    "verified_7d_30":     ("Verification", "Signed up in 30 days, verified within 7 days", "", ("Verified", "verified_at")),
    "verified_unknown_30": ("Verification", "Signed up in 30 days, verified, time not recorded",
                            "Verified before v9.12 started recording the time", ("Joined", "created_at")),
    "signed_in_7":        ("Usage", "Signed in, last 7 days", "Entered their PIN in the last 7 days", ("Last sign-in", "last_login")),
    "signed_in_30":       ("Usage", "Signed in, last 30 days", "Entered their PIN in the last 30 days", ("Last sign-in", "last_login")),
    "never_signed_in":    ("Usage", "No sign-in on record", "", ("Joined", "created_at")),
    "recipient_engaged_7":  ("Usage", "Alert recipients engaged, last 7 days", "Getting weekly alerts and opened one or used the site", None),
    "recipient_engaged_30": ("Usage", "Alert recipients engaged, last 30 days", "Getting weekly alerts and opened one or used the site", None),
    "reach_store":        ("Setup", "Verified and chose a store", "", None),
    "reach_list":         ("Setup", "Verified, store chosen and list started", "", None),
    "with_list":          ("Setup", "Has a list", "At least one item", None),
    "new_30":             ("Setup", "Signed up in the last 30 days", "", ("Joined", "created_at")),
}
GROUPS = ("Usage", "Email", "Verification", "Setup", "Data")


def verified(u) -> bool:
    return u.get("email_verified") is not False


def alerts_on(u) -> bool:
    return (u.get("prefs") or {}).get("email_enabled", True) is not False


def site_last(u):
    ds = [d for d in (parse(u.get("last_seen_at")), parse(u.get("last_login"))) if d]
    return max(ds) if ds else None


def mail_last(u):
    ds = [d for d in (parse(u.get("last_alert_open_at")), parse(u.get("last_alert_click_at"))) if d]
    return max(ds) if ds else None


def route(u, now, days: int) -> str:
    """'both' | 'email' | 'site' | 'none' for the last `days` days."""
    cut = now - timedelta(days=days)
    s, m = site_last(u), mail_last(u)
    s_on, m_on = bool(s and s >= cut), bool(m and m >= cut)
    return "both" if s_on and m_on else "site" if s_on else "email" if m_on else "none"


def has_list(p: dict) -> bool:
    """At least one real item (blank strings don't count). Used everywhere a list is checked."""
    return any(isinstance(i, str) and i.strip() for i in (p.get("items") or []))


def recipient(u) -> bool:
    p = u.get("prefs") or {}
    return verified(u) and bool(p.get("store_id")) and has_list(p) and alerts_on(u)


def reminders_finished(u, now) -> bool:
    if int(u.get("verify_reminders") or 0) < 4:
        return False
    last = parse(u.get("last_verify_reminder_at"))
    return bool(last and now - last >= timedelta(days=7))


def alerts_off_at(u):
    """When alerts were turned off, best record available."""
    return parse(u.get("alerts_off_at")) or parse(u.get("unsubscribed_at")) or parse(u.get("complained_at"))


def tags(u: dict, now) -> list:
    """Every group this account belongs to."""
    if not u.get("email") or u.get("email") == "__meta__":
        return []
    out = []
    p = u.get("prefs") or {}
    created = parse(u.get("created_at"))
    for days in (7, 30):
        r = route(u, now, days)
        out.append({"both": f"use_both_{days}", "email": f"use_email_{days}",
                    "site": f"use_site_{days}", "none": f"use_none_{days}"}[r])
        if r != "none":
            out.append(f"engaged_{days}")
    if recipient(u):
        out.append("alert_recipient")
        if route(u, now, 30) == "none":
            out.append("recipient_unengaged_30")
        else:
            out.append("recipient_engaged_30")
        if route(u, now, 7) != "none":
            out.append("recipient_engaged_7")
    login = parse(u.get("last_login"))
    if login and login >= now - timedelta(days=7):
        out.append("signed_in_7")
    if login and login >= now - timedelta(days=30):
        out.append("signed_in_30")
    if not login:
        out.append("never_signed_in")
    if route(u, now, 60) == "none" and not (created and created > now - timedelta(days=60)):
        out.append("dormant_60")                      # (an account younger than 60 days isn't dormant)

    if not alerts_on(u):
        out.append("alerts_off")
        off = alerts_off_at(u)
        if off and off >= now - timedelta(days=7):
            out.append("unsub_7")
    c = parse(u.get("complained_at"))
    if c:
        out.append("spam")
        if c >= now - timedelta(days=7):
            out.append("spam_7")
    if u.get("email_bounced_at"):
        out.append("bounced")

    if verified(u):
        out.append("verified")
        vat = parse(u.get("verified_at"))
        if created and created >= now - timedelta(days=30) and vat:
            if vat - created <= timedelta(hours=24):
                out.append("verified_24h_30")
            if vat - created <= timedelta(days=7):
                out.append("verified_7d_30")
        elif created and created >= now - timedelta(days=30):
            out.append("verified_unknown_30")
    if not verified(u):
        out.append("unverified")
        if created and created >= now - timedelta(days=30):
            out.append("unverified_new_30")
        if reminders_finished(u, now):
            out.append("unverified_finished")
        elif alerts_on(u):
            out.append("unverified_reminding")
        if u.get("verify_emails") is not None and int(u.get("verify_emails") or 0) == 0:
            out.append("unverified_no_email")
        if not alerts_on(u):
            out.append("unverified_unsub")

    if created and created >= now - timedelta(days=7):
        out.append("new_7")
    if created and created >= now - timedelta(days=30):
        out.append("new_30")
    if not verified(u):
        out.append("stop_verify")
    elif not p.get("store_id"):
        out.append("stop_store")
    elif not has_list(p):
        out.append("stop_list")
    elif not alerts_on(u):
        out.append("stop_alerts")
    if verified(u) and p.get("store_id"):
        out.append("reach_store")
        if has_list(p):
            out.append("reach_list")
    if not p.get("store_id"):
        out.append("no_store")
    if has_list(p):
        out.append("with_list")
    else:
        out.append("empty_list")
    if u.get("history_gaps"):
        out.append("history_gaps")
    return out


def counts(users, now) -> dict:
    c = {k: 0 for k in CATALOG}
    for u in users:
        for t in tags(u, now):
            c[t] += 1
    return c


def catalog_json() -> list:
    """For the browser: labels, groups and the extra column for each group."""
    return [{"key": k, "group": g, "label": l, "help": h,
             "column": ({"header": col[0], "field": col[1]} if col else None)}
            for k, (g, l, h, col) in CATALOG.items()]
