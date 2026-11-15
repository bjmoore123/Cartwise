"""shared/usage.py — who is using Cartwise, and how (v9.13).

Two routes count as "using Cartwise", and either is fine:
  site   signed in OR used the site with a saved session   -> users.last_seen_at
  email  opened or clicked a weekly deal alert              -> users.last_alert_open_at / last_alert_click_at

Each is recorded at most once per user per Eastern-time day as a daily total, so the
admin report can show how many people used each route per day without scanning logs:
  metrics pk = day#YYYY-MM-DD   site_users, alert_openers   (kept 400 days)
Pure helpers + two small writers that never raise (usage tracking must never break a request).
"""
from datetime import datetime, timezone, timedelta

DAY_ROW_DAYS = 400
MAX_DAYS = 1000                             # site-use days kept per account (~3 years of daily use)
ALERT_VIAS = ("settings", "unsubscribe link", "spam complaint", "admin")
SEEN_EVERY = timedelta(minutes=30)          # refresh last_seen_at at most this often per user


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = datetime(year, month, 1).weekday()            # Monday = 0 ... Sunday = 6
    return 1 + (6 - first) % 7 + 7 * (n - 1)


def eastern(dt: datetime) -> datetime:
    """UTC -> US Eastern without the time zone database (absent on Windows, not guaranteed on
    Lambda). Daylight time: 07:00 UTC on the 2nd Sunday of March to 06:00 UTC on the 1st Sunday
    of November."""
    u = dt.astimezone(timezone.utc)
    start = datetime(u.year, 3, _nth_sunday(u.year, 3, 2), 7, tzinfo=timezone.utc)
    end = datetime(u.year, 11, _nth_sunday(u.year, 11, 1), 6, tzinfo=timezone.utc)
    return u - timedelta(hours=4 if start <= u < end else 5)


def day_key(dt: datetime) -> str:
    return eastern(dt).strftime("%Y-%m-%d")


def parse(value):
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _bump_day(metrics_table, field: str, now: datetime) -> None:
    metrics_table.update_item(
        Key={"pk": f"day#{day_key(now)}"},
        UpdateExpression="ADD #f :one SET expires_at = if_not_exists(expires_at, :exp)",
        ExpressionAttributeNames={"#f": field},
        ExpressionAttributeValues={":one": 1, ":exp": int((now + timedelta(days=DAY_ROW_DAYS)).timestamp())})


def touch_site(users_table, metrics_table, user: dict, now=None) -> None:
    """Called on every signed-in request (helpers.get_session already has the account row).
    Writes only when last_seen_at is older than SEEN_EVERY; counts the user once per day."""
    try:
        now = now or datetime.now(timezone.utc)
        email = user.get("email")
        if not email or email in ("__meta__", "__admin__"):
            return
        last = parse(user.get("last_seen_at"))
        if last and now - last < SEEN_EVERY:
            return
        users_table.update_item(
            Key={"email": email}, UpdateExpression="SET last_seen_at = :now",
            ConditionExpression="attribute_exists(email)", ExpressionAttributeValues={":now": now.isoformat()})
        user["last_seen_at"] = now.isoformat()
        today = day_key(now)
        days = list(user.get("site_day_list") or [])
        if today not in days:
            # v9.14: every day this person used the site, kept on the account (the timeline's source).
            # Kept to the last MAX_DAYS days used; site_days stays the all-time count.
            days = (days + [today])[-MAX_DAYS:]
            users_table.update_item(Key={"email": email}, UpdateExpression="SET site_day_list = :d ADD site_days :one",
                                    ExpressionAttributeValues={":d": days, ":one": 1})
            user["site_day_list"] = days
            _bump_day(metrics_table, "site_users", now)
    except Exception as e:
        print(f"[CW] usage.touch_site: {e}")


def credit_alert(users_table, metrics_table, account: str, event: str, when: str, now=None) -> None:
    """A weekly deal alert was opened or clicked: credit the account it was sent for."""
    try:
        now = now or datetime.now(timezone.utc)
        field = "last_alert_click_at" if event == "email.clicked" else "last_alert_open_at"
        when_dt = parse(when) or now
        resp = users_table.update_item(
            Key={"email": account},
            UpdateExpression=f"SET {field} = :w",
            ConditionExpression=f"attribute_exists(email) AND (attribute_not_exists({field}) OR {field} < :w)",
            ExpressionAttributeValues={":w": when_dt.isoformat()},
            ReturnValues="UPDATED_OLD")
        if field == "last_alert_open_at":
            old = parse((resp.get("Attributes") or {}).get(field))
            if not old or day_key(old) != day_key(when_dt):
                users_table.update_item(Key={"email": account}, UpdateExpression="ADD alert_open_days :one",
                                        ExpressionAttributeValues={":one": 1})
                _bump_day(metrics_table, "alert_openers", when_dt)
    except Exception as e:
        if "ConditionalCheckFailed" not in str(e):
            print(f"[CW] usage.credit_alert: {e}")


def alerts_change(old_enabled, new_enabled, via: str, now=None):
    """(UpdateExpression fragment, values) recording when and how weekly alerts were turned
    off or back on (v9.14), or ("", {}) if nothing changed. `old_enabled` None = unknown/default on."""
    now = now or datetime.now(timezone.utc)
    was_on, is_on = old_enabled is not False, new_enabled is not False
    if was_on == is_on:
        return "", {}
    if not is_on:
        return "alerts_off_at = :ao_t, alerts_off_via = :ao_v", {":ao_t": now.isoformat(),
                                                                   ":ao_v": via if via in ALERT_VIAS else "settings"}
    return "alerts_on_at = :ao_t, alerts_on_via = :ao_v", {":ao_t": now.isoformat(),
                                                             ":ao_v": via if via in ALERT_VIAS else "settings"}
