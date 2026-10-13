"""tools/history_audit.py — inspect and repair weekly deal-history snapshots (v9.6).

Read-only unless you pass --yes. Every write first saves a JSON backup of the
rows it touches to ./history-backup-<store>-<week>-<timestamp>.json.

  python tools/history_audit.py --store 1546                      # one line per week, outliers flagged
  python tools/history_audit.py --store 1546 --week 2026-03-30    # detail for one week
  python tools/history_audit.py --store 1546 --week 2026-03-30 --dedupe          # dry run
  python tools/history_audit.py --store 1546 --week 2026-03-30 --dedupe --yes    # rewrite without duplicates
  python tools/history_audit.py --store 1546 --week 2026-03-30 --delete --yes    # remove the week

Add --env staging for the cartwise-staging-* tables. After any write the
history summary (sk "_summary") is deleted, so the next page load rebuilds it.

Why: the 2026-03-30 snapshot at store 1546 holds 3,257 deals, about twice a
normal week. Staging was writing to the production tables until 2026-10-12, so
the same week was probably written twice under one key. A doubled week inflates
"Rarely on sale" and frequency counts very little (they count weeks, not rows),
but it skews row-based stats and slows the summary build.
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone

CHUNK_SIZE = 200


def table_name(env: str) -> str:
    return "cartwise-staging-deal-history" if env == "staging" else "cartwise-deal-history"


def load_store(tbl, store: str) -> dict:
    """{week: {"index": row, "chunks": {i: row}}} plus summary rows under "_summary"."""
    from boto3.dynamodb.conditions import Key
    rows, kw = [], {"KeyConditionExpression": Key("store_id").eq(store)}
    while True:
        r = tbl.query(**kw)
        rows.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    weeks: dict = {}
    for row in rows:
        sk = str(row["sk"])
        if sk.startswith("_"):
            weeks.setdefault("_summary", {"index": None, "chunks": {}})["chunks"][sk] = row
            continue
        wk, sep, idx = sk.rpartition("#")
        if sep and idx.isdigit():
            weeks.setdefault(wk, {"index": None, "chunks": {}})["chunks"][int(idx)] = row
        else:
            weeks.setdefault(sk, {"index": None, "chunks": {}})["index"] = row
    return weeks


def week_deals(w: dict) -> list:
    out = []
    for i in sorted(w["chunks"]):
        out.extend(json.loads(w["chunks"][i].get("deals", "[]")))
    return out


def summarize(w: dict) -> dict:
    deals = week_deals(w)
    keys = Counter((d.get("title", ""), d.get("savings", "")) for d in deals)
    ids = Counter(d.get("id", "") for d in deals if d.get("id"))
    idx = w["index"] or {}
    return {
        "index_count": int(idx.get("count", 0) or 0),
        "num_chunks": int(idx.get("num_chunks", 0) or 0),
        "chunk_rows": len(w["chunks"]),
        "rows": len(deals),
        "unique": len(keys),
        "dup_ids": sum(c - 1 for c in ids.values() if c > 1),
        "fetched_at": str(idx.get("fetched_at", "")),
        "orphan": not w["index"],
    }


def backup(store: str, week: str, w: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = f"history-backup-{store}-{week}-{stamp}.json"
    rows = ([w["index"]] if w["index"] else []) + [w["chunks"][k] for k in sorted(w["chunks"])]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, default=str, indent=1)
    return path


def delete_rows(tbl, store: str, w: dict):
    if w["index"]:
        tbl.delete_item(Key={"store_id": store, "sk": w["index"]["sk"]})
    for row in w["chunks"].values():
        tbl.delete_item(Key={"store_id": store, "sk": row["sk"]})


def drop_summary(tbl, store: str, weeks: dict):
    for row in (weeks.get("_summary") or {"chunks": {}})["chunks"].values():
        tbl.delete_item(Key={"store_id": store, "sk": row["sk"]})


def dedupe_week(tbl, store: str, week: str, w: dict) -> tuple:
    """Rewrite the week keeping the first copy of each deal (by id, else title+savings)."""
    seen, keep = set(), []
    for d in week_deals(w):
        k = d.get("id") or (d.get("title", ""), d.get("savings", ""))
        if k in seen:
            continue
        seen.add(k)
        keep.append(d)
    idx = dict(w["index"] or {"store_id": store, "sk": week, "week": week})
    expires = idx.get("expires_at")
    fetched = idx.get("fetched_at", datetime.now(timezone.utc).isoformat())
    delete_rows(tbl, store, w)
    chunks = [keep[i:i + CHUNK_SIZE] for i in range(0, max(len(keep), 1), CHUNK_SIZE)]
    for i, chunk in enumerate(chunks):
        item = {"store_id": store, "sk": f"{week}#{i}", "fetched_at": fetched, "deals": json.dumps(chunk),
                "count": len(chunk), "num_chunks": len(chunks), "chunk_index": i}
        if expires:
            item["expires_at"] = expires
        tbl.put_item(Item=item)
    idx.update({"count": len(keep), "num_chunks": len(chunks)})
    tbl.put_item(Item=idx)
    return len(week_deals(w)), len(keep)


def main(argv=None, tbl=None, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", required=True)
    ap.add_argument("--env", choices=["production", "staging"], default="production")
    ap.add_argument("--week")
    ap.add_argument("--dedupe", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--yes", action="store_true", help="actually write (default is a dry run)")
    a = ap.parse_args(argv)
    if tbl is None:
        import boto3
        tbl = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")).Table(table_name(a.env))

    weeks = load_store(tbl, a.store)
    real = {k: v for k, v in weeks.items() if k != "_summary"}
    if not real:
        out(f"No history for store {a.store} in {table_name(a.env)}")
        return 1
    stats = {k: summarize(v) for k, v in real.items()}
    median = statistics.median([s["rows"] for s in stats.values()]) or 1

    if not a.week:
        out(f"{'week':<12} {'rows':>6} {'unique':>6} {'dupIDs':>6} {'chunks':>6}  note")
        for k in sorted(stats):
            s = stats[k]
            notes = []
            if s["rows"] > 1.5 * median:
                notes.append(f"OUTLIER {s['rows'] / median:.1f}x median")
            if s["dup_ids"]:
                notes.append(f"{s['dup_ids']} duplicate ids")
            if s["orphan"]:
                notes.append("no index row")
            if s["index_count"] and s["index_count"] != s["rows"]:
                notes.append(f"index says {s['index_count']}")
            out(f"{k:<12} {s['rows']:>6} {s['unique']:>6} {s['dup_ids']:>6} {s['chunk_rows']:>6}  {'; '.join(notes)}")
        out(f"\n{len(stats)} weeks, median {median:.0f} deals/week. Use --week W for detail.")
        return 0

    if a.week not in real:
        out(f"Week {a.week} not found. Weeks: {', '.join(sorted(real))}")
        return 1
    w, s = real[a.week], stats[a.week]
    types = Counter(d.get("saving_type", "?") for d in week_deals(w))
    out(json.dumps({"week": a.week, **s, "median_rows": median, "saving_types": types}, indent=1, default=str))

    if not (a.dedupe or a.delete):
        return 0
    action = "dedupe" if a.dedupe else "delete"
    if not a.yes:
        out(f"\nDry run: would {action} week {a.week} ({s['rows']} rows"
            + (f" -> {s['unique'] if not s['dup_ids'] else s['rows'] - s['dup_ids']} kept)" if a.dedupe else ")")
            + ". Re-run with --yes to apply.")
        return 0
    path = backup(a.store, a.week, w)
    out(f"Backup written: {path}")
    if a.dedupe:
        before, after = dedupe_week(tbl, a.store, a.week, w)
        out(f"Rewrote {a.week}: {before} -> {after} deals")
    else:
        delete_rows(tbl, a.store, w)
        out(f"Deleted week {a.week} ({s['rows']} deals)")
    drop_summary(tbl, a.store, weeks)
    out("History summary cleared; it rebuilds on the next page load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
