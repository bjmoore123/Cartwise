"""shared/adweek.py — a store's weekly ad dates, read from its deals (v9.15.1).

Publix runs two cycles (Wednesday–Tuesday and Thursday–Wednesday) and moves them around holidays,
so the dates come from the deals themselves, never from the weekday. Pure functions; no AWS.
"""
from collections import Counter
from datetime import date, datetime

AD_TYPES = ("WeeklyAd", "Stacked", "StackedDeals", "ExtraSavings")


def parse(vf, today: date):
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


def _ad_deals(deals):
    return [d for d in deals if (d.get("saving_type") or "WeeklyAd") in AD_TYPES]


def start(deals, today: date | None = None):
    """The day the store's current ad started (most common real valid_from), or None."""
    today = today or date.today()
    votes = Counter(dt for dt in (parse(d.get("valid_from"), today) for d in _ad_deals(deals)) if dt)
    if not votes:                                   # nothing usable: any deal with a real date
        votes = Counter(dt for dt in (parse(d.get("valid_from"), today) for d in deals) if dt)
    return votes.most_common(1)[0][0] if votes else None


def window(deals, today: date | None = None):
    """(start, end) of the store's current ad, or None. End = the most common valid_thru among the
    ad's deals: what most of the week's deals are good through."""
    today = today or date.today()
    s = start(deals, today)
    if not s:
        return None
    ends = Counter(e for e in (parse(d.get("valid_thru"), today) for d in _ad_deals(deals)
                               if parse(d.get("valid_from"), today) == s) if e and e >= s)
    return (s, ends.most_common(1)[0][0]) if ends else None


def label(d: date) -> str:
    """Thu 11/26"""
    return f"{d:%a} {d.month}/{d.day}"


def span(win) -> str:
    """Thu 11/26 – Wed 12/2"""
    return f"{label(win[0])} – {label(win[1])}" if win else ""


def same_as(m: dict, win, today: date | None = None) -> bool:
    """True when this deal runs exactly the ad's dates (so its row needn't repeat them)."""
    if not win:
        return False
    today = today or date.today()
    return parse(m.get("valid_from"), today) == win[0] and parse(m.get("valid_thru"), today) == win[1]
