"""shared/mailmeta.py — labels on outgoing email, so delivery/open/bounce reports can be
grouped by type (v9.12).

Every send carries two Resend tags:
  kind = weekly | verify | reminder | pin_reset | pin_changed | test | admin_report | feedback
  env  = prod | staging   (both environments send through one Resend account, and Resend
                           reports events to one webhook; env lets each side ignore the other's)
"""
import os
from datetime import datetime, timezone, timedelta

KINDS = ("weekly", "verify", "reminder", "pin_reset", "pin_changed", "test", "admin_report", "feedback")
KIND_LABELS = {
    "weekly": "Weekly deal alert", "verify": "Verify email", "reminder": "Verification reminder",
    "pin_reset": "PIN reset", "pin_changed": "PIN changed", "test": "Test email",
    "admin_report": "Admin report", "feedback": "Feedback notice", "other": "Other",
}


def env_name() -> str:
    return "staging" if "staging" in os.environ.get("USERS_TABLE", "") else "prod"


def tags(kind: str) -> list:
    """Resend `tags` for a message. Values may only use letters, digits, - and _."""
    return [{"name": "kind", "value": kind if kind in KINDS else "other"},
            {"name": "env", "value": env_name()}]


def kind_from_subject(subject: str) -> str:
    """Best guess for mail sent before tags existed (and for older log rows)."""
    s = (subject or "").lower()
    if "reminder" in s and "verify" in s:           return "reminder"
    if "verify your cartwise email" in s:           return "verify"
    if "reset your cartwise pin" in s:              return "pin_reset"
    if "pin was changed" in s:                      return "pin_changed"
    if "test" in s:                                 return "test"
    if "weekly report" in s:                        return "admin_report"
    if "feedback" in s:                             return "feedback"
    if "on sale" in s or "on your list" in s:       return "weekly"
    return "other"


ENGAGEMENT_KINDS = ("weekly",)       # opening/clicking these counts as "using Cartwise by email"
SEND_ROW_DAYS = 120


def message_id(resp) -> str:
    """Resend's id from an Emails.send() response (a dict in the Python SDK)."""
    if isinstance(resp, dict):
        return str(resp.get("id") or "")[:80]
    return str(getattr(resp, "id", "") or "")[:80]


def record_send(table, resp, kind: str, to: str, account: str = "") -> None:
    """v9.13: write em#<id> at send time with the ACCOUNT it was sent for. Resend's later
    events (delivered, opened, ...) merge into this row, so an open can be credited to the
    right account even when alerts go to a different notify-to address. Never raises."""
    eid = message_id(resp)
    if not eid or table is None:
        return
    now = datetime.now(timezone.utc)
    try:
        table.update_item(
            Key={"pk": f"em#{eid}"},
            UpdateExpression="SET account = :a, kind = if_not_exists(kind, :k), #to = if_not_exists(#to, :to), "
                             "sent_at = if_not_exists(sent_at, :now), first_seen = if_not_exists(first_seen, :now), "
                             "expires_at = :exp",
            ExpressionAttributeNames={"#to": "to"},
            ExpressionAttributeValues={":a": (account or to or "").lower()[:200], ":k": kind if kind in KINDS else "other",
                                       ":to": (to or "").lower()[:200], ":now": now.isoformat(),
                                       ":exp": int((now + timedelta(days=SEND_ROW_DAYS)).timestamp())})
    except Exception as e:
        print(f"[CW] record_send: {e}")
