#!/usr/bin/env python3
"""
delete_users.py — Delete CartWise users whose email matches a regex pattern.

Usage:
    python3 delete_users.py "<pattern>" [--dry-run] [--yes]

Examples:
    python3 delete_users.py "weepingsunshine"           # any email containing that string
    python3 delete_users.py ".*\\+.*@gmail\\.com"       # + in local part of gmail address
    python3 delete_users.py ".*@.*\\+.*"                # + anywhere in domain part
    python3 delete_users.py ".*@example\\.com" --dry-run

Options:
    --dry-run   List matches without deleting anything
    --yes       Skip confirmation prompt (for scripting)
"""

import sys
import re
import boto3
from decimal import Decimal

REGION = "us-east-1"
APP    = "cartwise"

def dec(obj):
    if isinstance(obj, Decimal): return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):    return {k: dec(v) for k, v in obj.items()}
    if isinstance(obj, list):    return [dec(i) for i in obj]
    return obj

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    pattern      = args[0]
    dry_run      = "--dry-run" in args
    skip_confirm = "--yes" in args

    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        print(f"ERROR: Invalid regex pattern: {e}")
        sys.exit(1)

    dynamo       = boto3.resource("dynamodb", region_name=REGION)
    users_tbl    = dynamo.Table(f"{APP}-users")
    sessions_tbl = dynamo.Table(f"{APP}-sessions")

    print(f"Scanning {APP}-users...")
    items, resp = [], users_tbl.scan(ProjectionExpression="email, prefs, created_at")
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = users_tbl.scan(
            ProjectionExpression="email, prefs, created_at",
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))

    users = [dec(u) for u in items if u.get("email") != "__meta__"]
    print(f"Total users: {len(users)}")

    matches = [u for u in users if rx.search(u.get("email", ""))]

    if not matches:
        print(f"\nNo users matched pattern: {pattern}")
        return

    print(f"\nMatched {len(matches)} user(s) for pattern /{pattern}/:")
    for u in matches:
        p = u.get("prefs") or {}
        store    = p.get("store_name") or p.get("store_id") or "no store"
        items_ct = len(p.get("items") or [])
        notify   = p.get("notify_email", "")
        notify_str = f" → notify:{notify}" if notify and notify != u["email"] else ""
        print(f"  {u['email']}  [{store}, {items_ct} items]{notify_str}  created:{(u.get('created_at') or '')[:10]}")

    if dry_run:
        print("\n(dry-run — no deletions performed)")
        return

    if not skip_confirm:
        answer = input(f"\nDelete these {len(matches)} user(s)? Type 'yes' to confirm: ").strip()
        if answer.lower() != "yes":
            print("Aborted.")
            return

    deleted = 0
    errors  = []
    for u in matches:
        email = u["email"]
        try:
            users_tbl.delete_item(Key={"email": email})
            # Clean up active sessions for this user
            sess_resp = sessions_tbl.scan(
                FilterExpression="email = :e",
                ExpressionAttributeValues={":e": email},
                ProjectionExpression="#tok",
                ExpressionAttributeNames={"#tok": "token"},
            )
            for s in sess_resp.get("Items", []):
                sessions_tbl.delete_item(Key={"token": s["token"]})
            deleted += 1
            print(f"  ✓ Deleted {email}")
        except Exception as e:
            errors.append(email)
            print(f"  ✗ Failed {email}: {e}")

    print(f"\nDone. {deleted} deleted, {len(errors)} errors.")
    if errors:
        print(f"Errors: {errors}")

if __name__ == "__main__":
    main()
