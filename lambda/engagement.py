"""lambda/engagement.py — what happened to each email after we sent it (v9.12).

Resend calls our existing webhook (POST /inbound/email-list, already signature-checked)
with email.sent / delivered / delivery_delayed / bounced / complained / opened / clicked.
One row per message in the metrics table, keyed by Resend's email id:

    pk = em#<email_id>   kind, to, sent_at, delivered_at, opened_at, clicked_at,
                         bounced_at (+ bounce_type), complained_at, opens, clicks
                         (first time each thing happened; kept 120 days)

Counting unique messages (not raw events) is what makes "open rate" mean something: one
person opening a mail five times is one open.

Two things act on the account, because ignoring them hurts deliverability for everyone:
  - spam complaint  -> the account is unsubscribed (same as clicking Unsubscribe)
  - permanent bounce -> the account is flagged (email_bounced_at) and shown in Admin
Nothing here trusts the payload beyond the signature: values are length-capped, the kind
comes from a fixed list, and events tagged for the other environment are ignored.
"""
import os
from datetime import datetime, timezone, timedelta

from helpers import ok, users_table, dynamodb, _to_py, DEFAULT_PREFS
from logging_utils import _log_app_event
from shared import mailmeta

metrics_tbl = dynamodb.Table(os.environ.get("METRICS_TABLE", "cartwise-metrics"))

EVENTS = {
    "email.sent": "sent_at", "email.delivered": "delivered_at", "email.delivery_delayed": "delayed_at",
    "email.bounced": "bounced_at", "email.complained": "complained_at",
    "email.opened": "opened_at", "email.clicked": "clicked_at",
}
KEEP_DAYS = 120


def is_engagement_event(evt_type: str) -> bool:
    return evt_type in EVENTS


def _tags(data: dict) -> dict:
    t = data.get("tags") or {}
    if isinstance(t, list):                                   # [{"name":..,"value":..}] form
        t = {str(x.get("name", "")): str(x.get("value", "")) for x in t if isinstance(x, dict)}
    return t if isinstance(t, dict) else {}


def record_event(body: dict):
    """Store one Resend email event. Always answers 200 so Resend doesn't retry forever."""
    evt = body.get("type", "")
    data = body.get("data") or {}
    email_id = str(data.get("email_id") or "")[:80]
    if evt not in EVENTS or not email_id:
        return ok({"skipped": True})
    tags = _tags(data)
    if tags.get("env") and tags["env"] != mailmeta.env_name():
        return ok({"skipped": True, "reason": "other environment"})

    kind = tags.get("kind") or mailmeta.kind_from_subject(str(data.get("subject") or ""))
    if kind not in mailmeta.KINDS:
        kind = "other"
    to = data.get("to") or []
    to = str(to[0] if isinstance(to, list) and to else to or "")[:200].lower()
    when = str(body.get("created_at") or datetime.now(timezone.utc).isoformat())[:40]
    now = datetime.now(timezone.utc)
    field = EVENTS[evt]

    sets = [f"{field} = if_not_exists({field}, :when)", "kind = if_not_exists(kind, :k)",
            "#to = if_not_exists(#to, :to)", "first_seen = if_not_exists(first_seen, :now)",
            "expires_at = :exp"]
    names = {"#to": "to"}
    vals = {":when": when, ":k": kind, ":to": to, ":now": now.isoformat(),
            ":exp": int((now + timedelta(days=KEEP_DAYS)).timestamp())}
    adds = []
    if evt == "email.opened":
        adds.append("opens :one")
    if evt == "email.clicked":
        adds.append("clicks :one")
    if adds:
        vals[":one"] = 1
    if evt == "email.bounced":
        b = data.get("bounce") or {}
        sets.append("bounce_type = :bt")
        vals[":bt"] = str(b.get("type") or b.get("subType") or "")[:40] if isinstance(b, dict) else ""
    expr = "SET " + ", ".join(sets) + (" ADD " + ", ".join(adds) if adds else "")
    try:
        metrics_tbl.update_item(Key={"pk": f"em#{email_id}"}, UpdateExpression=expr,
                                ExpressionAttributeNames=names, ExpressionAttributeValues=vals)
    except Exception as e:
        print(f"[CW] engagement: could not record {evt}: {e}")
        return ok({"recorded": False})

    if evt == "email.complained":
        _on_complaint(to, kind)
    elif evt == "email.bounced" and "perm" in vals.get(":bt", "").lower():
        _on_hard_bounce(to, kind)
    return ok({"recorded": True})


def _account_for(address: str):
    """The account this address belongs to: the sign-in email, else nothing. (A notify-to
    address that differs from the sign-in email isn't looked up: that needs a table scan.)"""
    if not address:
        return None
    return users_table.get_item(Key={"email": address}).get("Item")


def _on_complaint(address: str, kind: str):
    """Marked as spam: stop scheduled mail to this account, exactly like Unsubscribe."""
    try:
        item = _account_for(address)
        if not item:
            return
        prefs = _to_py(item.get("prefs") or {}) or {**DEFAULT_PREFS, "notify_email": address}
        prefs["email_enabled"] = False
        now = datetime.now(timezone.utc).isoformat()
        users_table.update_item(
            Key={"email": address},
            UpdateExpression="SET prefs = :p, complained_at = :t, unsubscribed_at = if_not_exists(unsubscribed_at, :t)",
            ExpressionAttributeValues={":p": prefs, ":t": now})
        _log_app_event("email", "warn", action="spam-complaint", user=address, kind=kind,
                       message="Marked as spam; account unsubscribed")
    except Exception as e:
        print(f"[CW] engagement: complaint handling failed: {e}")


def _on_hard_bounce(address: str, kind: str):
    """The mailbox doesn't exist: flag the account so it shows up in Admin."""
    try:
        if not _account_for(address):
            return
        users_table.update_item(
            Key={"email": address}, UpdateExpression="SET email_bounced_at = :t",
            ExpressionAttributeValues={":t": datetime.now(timezone.utc).isoformat()})
        _log_app_event("email", "warn", action="hard-bounce", user=address, kind=kind,
                       message="Permanent bounce; address flagged")
    except Exception as e:
        print(f"[CW] engagement: bounce handling failed: {e}")
