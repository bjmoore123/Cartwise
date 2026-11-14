"""shared/mailmeta.py — labels on outgoing email, so delivery/open/bounce reports can be
grouped by type (v9.12).

Every send carries two Resend tags:
  kind = weekly | verify | reminder | pin_reset | pin_changed | test | admin_report | feedback
  env  = prod | staging   (both environments send through one Resend account, and Resend
                           reports events to one webhook; env lets each side ignore the other's)
"""
import os

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
