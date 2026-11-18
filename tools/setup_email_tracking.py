"""tools/setup_email_tracking.py — turn on Resend open tracking for cartwise.shopping.

Resend only tracks opens once the domain has a verified tracking subdomain (a CNAME such as
links.cartwise.shopping that Resend gives us). This script:

  1. sets tracking_subdomain (default "links"), open_tracking on, click_tracking OFF in Resend
  2. reads the CNAME record Resend asks for and adds it to our Route 53 zone (UPSERT)
  3. asks Resend to verify the domain, then shows the record's status

    python tools/setup_email_tracking.py              # dry run: shows what it would do
    python tools/setup_email_tracking.py --apply      # does it
    python tools/setup_email_tracking.py --status     # later: is the tracking record verified yet?

One Resend domain serves production and staging, so run it once. Click tracking stays off: it
would send every link (PIN reset, verify) through Resend first. The Resend API key comes from SSM
(/publix/resend-api-key) and is never printed. Needs a full-access Resend key to change the domain.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import boto3

REGION = "us-east-1"
DOMAIN = "cartwise.shopping"


def resend(key, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.resend.com" + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "User-Agent": "cartwise-setup/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode() or "{}"
            return json.loads(txt)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:300]}


def fqdn(name):
    name = (name or "").rstrip(".")
    return name if name == DOMAIN or name.endswith("." + DOMAIN) else f"{name}.{DOMAIN}"


def tracking_records(dom, sub):
    """The CNAME(s) Resend lists for the tracking subdomain."""
    want = f"{sub}.{DOMAIN}"
    out = []
    for r in dom.get("records") or []:
        if str(r.get("type", "")).upper() != "CNAME":
            continue
        name = fqdn(r.get("name"))
        if name == want or "track" in str(r.get("record", "")).lower():
            out.append({"name": name, "value": str(r.get("value", "")).rstrip("."),
                        "status": r.get("status"), "record": r.get("record")})
    return out


def show(dom, sub):
    print(f"   {DOMAIN}: status={dom.get('status')}  open_tracking={dom.get('open_tracking')}  "
          f"click_tracking={dom.get('click_tracking')}  tracking_subdomain={dom.get('tracking_subdomain')}")
    for t in tracking_records(dom, sub):
        print(f"   tracking record: {t['name']}  CNAME  {t['value']}  status={t['status']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--subdomain", default="links")
    a = ap.parse_args()
    sub = a.subdomain.strip(".").lower()

    key = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/publix/resend-api-key", WithDecryption=True)["Parameter"]["Value"]
    doms = resend(key, "GET", "/domains")
    if "_error" in doms:
        sys.exit(f"Can't list Resend domains: {doms['_error']}")
    match = [d for d in doms.get("data", []) if d.get("name") == DOMAIN]
    if not match:
        sys.exit(f"{DOMAIN} not found in Resend")
    did = match[0]["id"]
    dom = resend(key, "GET", f"/domains/{did}")

    print("\nNow")
    show(dom, sub)
    if a.status:
        return

    r53 = boto3.client("route53")
    zones = r53.list_hosted_zones_by_name(DNSName=DOMAIN + ".").get("HostedZones", [])
    zone = next((z for z in zones if z["Name"] == DOMAIN + "." and not z["Config"].get("PrivateZone")), None)
    if not zone:
        sys.exit(f"No public Route 53 zone for {DOMAIN}")
    zid = zone["Id"].split("/")[-1]
    existing = r53.list_resource_record_sets(HostedZoneId=zid, StartRecordName=f"{sub}.{DOMAIN}.",
                                             MaxItems="5")["ResourceRecordSets"]
    clash = [x for x in existing if x["Name"].rstrip(".") == f"{sub}.{DOMAIN}" and x["Type"] != "CNAME"]
    if clash:
        sys.exit(f"{sub}.{DOMAIN} already has a {clash[0]['Type']} record; pick another --subdomain")

    print(f"\nPlan (Route 53 zone {zid})")
    print(f"   1. Resend: tracking_subdomain={sub}, open_tracking=true, click_tracking=false")
    print(f"   2. Route 53: UPSERT CNAME {sub}.{DOMAIN} → (the value Resend gives after step 1)")
    print("   3. Resend: verify the domain")
    if not a.apply:
        print("\nDry run. Add --apply to do it.")
        return

    print("\n1. Resend")
    r = resend(key, "PATCH", f"/domains/{did}",
               {"tracking_subdomain": sub, "open_tracking": True, "click_tracking": False})
    if "_error" in r:
        sys.exit(f"   ✗ {r['_error']}  (a sending-only API key can't change the domain: do step 1 in the dashboard)")
    print("   ✓ updated")

    recs = []
    for _ in range(10):                                  # the new record can take a moment to appear
        dom = resend(key, "GET", f"/domains/{did}")
        recs = tracking_records(dom, sub)
        if recs and all(t["value"] for t in recs):
            break
        time.sleep(3)
    if not recs:
        print("   ✗ Resend didn't list a tracking CNAME. Its records:")
        for x in dom.get("records") or []:
            print(f"     {x.get('record')}  {x.get('type')}  {x.get('name')}  {x.get('value')}")
        sys.exit("   Add the tracking record by hand in Route 53, then run with --status.")

    print("\n2. Route 53")
    changes = [{"Action": "UPSERT", "ResourceRecordSet": {"Name": t["name"] + ".", "Type": "CNAME", "TTL": 300,
                                                          "ResourceRecords": [{"Value": t["value"]}]}} for t in recs]
    ch = r53.change_resource_record_sets(HostedZoneId=zid, ChangeBatch={
        "Comment": "Resend open tracking (tools/setup_email_tracking.py)", "Changes": changes})
    for t in recs:
        print(f"   ✓ {t['name']}  CNAME  {t['value']}")
    cid = ch["ChangeInfo"]["Id"]
    for _ in range(30):
        if r53.get_change(Id=cid)["ChangeInfo"]["Status"] == "INSYNC":
            print("   ✓ live in Route 53")
            break
        time.sleep(5)

    print("\n3. Resend verify")
    v = resend(key, "POST", f"/domains/{did}/verify")
    print("   ✗ " + v["_error"] if "_error" in v else "   ✓ verification started")
    time.sleep(10)
    print("\nNow")
    show(resend(key, "GET", f"/domains/{did}"), sub)
    print("\nIf the tracking record isn't 'verified' yet, give it a few minutes and run --status.")


if __name__ == "__main__":
    sys.exit(main())
