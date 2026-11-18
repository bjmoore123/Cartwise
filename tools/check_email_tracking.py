"""tools/check_email_tracking.py — why aren't email opens showing up? (read-only)

    python tools/check_email_tracking.py --env prod
    python tools/check_email_tracking.py --env staging

Asks Resend three things and compares them with what our metrics table recorded:
  1. Is open tracking turned on for the sending domain?
  2. Is our webhook subscribed to email.opened?
  3. For the most recent emails we sent: what does Resend say happened to each one
     (last_event), and did our table record an open?

Changes nothing. The Resend API key is read from SSM (/publix/resend-api-key) and never printed.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

import boto3

REGION = "us-east-1"


def resend(key, path):
    req = urllib.request.Request("https://api.resend.com" + path,
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "cartwise-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["prod", "staging"], required=True)
    ap.add_argument("--n", type=int, default=12, help="how many recent emails to check")
    a = ap.parse_args()
    table = "cartwise-metrics" if a.env == "prod" else "cartwise-staging-metrics"

    key = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/publix/resend-api-key", WithDecryption=True)["Parameter"]["Value"]

    print("\n1. Sending domain")
    doms = resend(key, "/domains")
    if "_error" in doms:
        print("   could not list domains:", doms["_error"])
    for d in doms.get("data", []):
        full = resend(key, f"/domains/{d['id']}")
        ot, ct = full.get("open_tracking"), full.get("click_tracking")
        print(f"   {d.get('name')}: status={d.get('status')}  open_tracking={ot}  click_tracking={ct}")
        if ot is False:
            print("   ✗ Open tracking is OFF for this domain: turn it on in Resend → Domains → this domain.")

    print("\n2. Webhooks")
    hooks = resend(key, "/webhooks")
    if "_error" in hooks:
        print("   could not list webhooks (needs a full-access API key):", hooks["_error"])
    for h in hooks.get("data", []):
        ev = h.get("events") or []
        mark = "✓" if "email.opened" in ev else "✗ email.opened NOT subscribed"
        print(f"   {h.get('endpoint')}  status={h.get('status')}  {mark}")
        print(f"      events: {', '.join(ev) if ev else '(none listed)'}")

    print(f"\n3. Last {a.n} tracked emails ({table})")
    tbl = boto3.resource("dynamodb", region_name=REGION).Table(table)
    rows, kw = [], {"FilterExpression": "begins_with(pk, :p)", "ExpressionAttributeValues": {":p": "em#"}}
    while True:
        r = tbl.scan(**kw)
        rows += r.get("Items", [])
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    rows.sort(key=lambda x: str(x.get("sent_at") or x.get("first_seen") or ""), reverse=True)
    resend_opened = ours_opened = 0
    print(f"   {'kind':<13}{'sent (UTC)':<18}{'Resend last_event':<20}{'our opened_at':<22}to")
    for row in rows[:a.n]:
        eid = row["pk"][3:]
        e = resend(key, f"/emails/{eid}")
        last = e.get("last_event") or e.get("_error", "?")[:18]
        resend_opened += last in ("opened", "clicked")
        ours_opened += bool(row.get("opened_at"))
        to = str(row.get("to") or "")
        to = (to[:2] + "…@" + to.split("@")[-1]) if "@" in to else to
        print(f"   {str(row.get('kind')):<13}{str(row.get('sent_at') or row.get('first_seen'))[:16]:<18}"
              f"{last:<20}{str(row.get('opened_at') or '–')[:19]:<22}{to}")

    print("\nWhat it means")
    if resend_opened and not ours_opened:
        print("   Resend saw opens but our table has none → the webhook isn't sending email.opened to us (see 2).")
    elif not resend_opened:
        print("   Resend itself saw no opens → either open tracking is off (see 1), the emails were sent before it")
        print("   was turned on, or nobody has opened them with images showing (Gmail 'Ask before displaying")
        print("   external images' blocks the tracking pixel).")
    else:
        print("   Resend and our table agree: opens are being recorded.")


if __name__ == "__main__":
    sys.exit(main())
