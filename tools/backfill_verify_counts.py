"""tools/backfill_verify_counts.py — set each account's real verification-email count (v9.13).

From v9.13 the app counts every verification email Resend accepts (sign-up email, "resend"
from the banner, admin "Resend Verify", and the 4 reminders). This fills in the history
from before that, using the Lambda logs in CloudWatch (kept forever; the lines below have
been printed since email verification launched in March 2026):

    API:      [CW] Verification email sent -> <email>      (sign-up, resend, admin resend)
    scraper:  Reminder <n>/4 -> <email>                    (verification reminders)

Only successful sends print those lines, so failed attempts are not counted.

    python tools/backfill_verify_counts.py --env prod             # dry run: prints the table
    python tools/backfill_verify_counts.py --env prod --apply     # writes the counts
    python tools/backfill_verify_counts.py --env staging --apply

Safe to run again: it SETS each count from the full log history (which includes sends made
after v9.13 went live), so nothing is counted twice. Needs your AWS credentials (same as the
aws CLI). Read-only on the logs; writes only verify_emails / last_verify_email_at.
"""
import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import boto3

SIGNUP = re.compile(r"Verification email sent -> (\S+@\S+)")
REMINDER = re.compile(r"Reminder (\d)/\d -> (\S+@\S+)")


def scan_logs(logs, group: str, pattern: str, regex, key_group: int):
    """{email: [timestamps]} for every log line matching `pattern` in `group`."""
    found = defaultdict(list)
    kwargs = {"logGroupName": group, "filterPattern": pattern}
    pages = 0
    while True:
        try:
            resp = logs.filter_log_events(**kwargs)
        except logs.exceptions.ResourceNotFoundException:
            print(f"   (no log group {group})")
            return found
        for ev in resp.get("events", []):
            m = regex.search(ev.get("message", ""))
            if m:
                email = m.group(key_group).strip().lower().rstrip(".,;")
                found[email].append(datetime.fromtimestamp(ev["timestamp"] / 1000, tz=timezone.utc))
        pages += 1
        if pages % 20 == 0:
            print(f"   …{pages} pages read from {group}")
        tok = resp.get("nextToken")
        if not tok:
            return found
        kwargs["nextToken"] = tok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", choices=("prod", "staging"), required=True)
    ap.add_argument("--apply", action="store_true", help="write the counts (default: dry run)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    a = ap.parse_args()

    app = "cartwise" if a.env == "prod" else "cartwise-staging"
    session = boto3.session.Session(region_name=a.region)
    logs = session.client("logs")
    users = session.resource("dynamodb").Table(f"{app}-users")

    print(f"Reading CloudWatch logs for {app}-api and {app}-scraper (all history)…")
    signup = scan_logs(logs, f"/aws/lambda/{app}-api", '"Verification email sent"', SIGNUP, 1)
    remind = scan_logs(logs, f"/aws/lambda/{app}-scraper", '"Reminder"', REMINDER, 2)

    items, resp = [], users.scan(ProjectionExpression="email, email_verified, created_at, verify_emails, verify_reminders")
    items += resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = users.scan(ProjectionExpression="email, email_verified, created_at, verify_emails, verify_reminders",
                          ExclusiveStartKey=resp["LastEvaluatedKey"])
        items += resp.get("Items", [])
    items = [i for i in items if i.get("email") != "__meta__"]

    rows = []
    for u in items:
        e = u["email"].lower()
        s, r = signup.get(e, []), remind.get(e, [])
        # a reminder line is only printed after a successful send; log lines are never duplicated,
        # but a Lambda retry could repeat a send, so count what was actually sent
        total = len(s) + len(r)
        last = max(s + r) if (s or r) else None
        rows.append((e, u.get("email_verified") is not False, len(s), len(r), total, last,
                     int(u.get("verify_emails") or 0)))
    rows.sort(key=lambda x: (x[1], -x[4], x[0]))

    w = max([len(r[0]) for r in rows] + [7])
    print(f"\n{'Account':<{w}}  Verified  Sign-up/resend  Reminders  Total  Last sent         Now stored")
    for e, ver, s, r, total, last, cur in rows:
        print(f"{e:<{w}}  {'yes' if ver else 'NO ':<8}  {s:>14}  {r:>9}  {total:>5}  "
              f"{last.strftime('%Y-%m-%d %H:%M') if last else '—':<16}  {cur:>10}")
    unverified = [r for r in rows if not r[1]]
    print(f"\n{len(rows)} accounts; {len(unverified)} unverified, of which "
          f"{sum(1 for r in unverified if r[4] == 0)} have no verification email on record.")

    if not a.apply:
        print("\nDry run: nothing written. Add --apply to save these counts.")
        return 0
    n = 0
    now = datetime.now(timezone.utc).isoformat()
    for e, ver, s, r, total, last, cur in rows:
        expr, vals = "SET verify_emails = :n, verify_emails_backfilled_at = :now", {":n": total, ":now": now}
        if last:
            expr += ", last_verify_email_at = :last"
            vals[":last"] = last.isoformat()
        users.update_item(Key={"email": e}, UpdateExpression=expr, ExpressionAttributeValues=vals,
                          ConditionExpression="attribute_exists(email)")
        n += 1
    print(f"\nSaved counts for {n} accounts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
