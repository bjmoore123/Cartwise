"""v9.12: admin metrics, email engagement tracking, weekly admin report."""
import base64, hashlib, hmac, importlib.util, json, os, sys, time, types
from datetime import datetime, timezone, timedelta

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime(2026, 11, 19, 14, tzinfo=timezone.utc)            # a Thursday, 9 AM Eastern
ADMIN_TO = "admin-inbox@example.com"


def iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def user(email, days_ago, verified=True, store=True, items=("milk", "eggs"), enabled=True, login=None, **extra):
    prefs = {"store_id": "1234" if store else "", "store_name": "Publix at Biltmore, Asheville, NC" if store else "",
             "items": list(items), "email_enabled": enabled}
    u = {"email": email, "created_at": iso(days_ago), "email_verified": verified, "prefs": prefs, **extra}
    if login is not None:
        u["last_login"] = iso(login)
    return u


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=sent.append)))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("FEEDBACK_TO", ADMIN_TO)
    return sent


@pytest.fixture
def M(dynamodb_tables):
    import metrics
    return metrics


def sample():
    users = [
        user("a@x.com", 200, login=1),                                                    # old, active
        user("b@x.com", 100, login=20),                                                   # active in 30d only
        user("c@x.com", 90, login=80),                                                    # dormant
        user("d@x.com", 3, verified_at=iso(3, -2), verified_after_reminders=0, login=2),  # new, verified in 2h
        user("e@x.com", 20, verified_at=iso(12), verified_after_reminders=1, items=()),   # verified day 8, no list
        user("f@x.com", 5, verified=False, store=False, items=()),                        # new, unverified
        user("g@x.com", 40, verified=False, store=False, items=(), verify_reminders=4),   # stale unverified
        user("h@x.com", 10, enabled=False, unsubscribed_at=iso(2), verified_at=iso(9), verified_after_reminders=0),
        {"email": "__meta__", "deleted_count": 6},
    ]
    auth = [{"ts": iso(1), "success": True, "reason": ""}, {"ts": iso(2), "success": True, "reason": ""},
            {"ts": iso(2), "success": False, "reason": "bad_pin"}, {"ts": iso(3), "success": False, "reason": "locked_out"},
            {"ts": iso(3), "success": True, "reason": "pin_reset_requested"}, {"ts": iso(3), "success": True, "reason": "pin_reset"},
            {"ts": iso(9), "success": False, "reason": "bad_pin"}]                        # outside the window
    app = [{"ts": iso(1), "source": "email", "level": "info", "ok": True, "kind": "weekly", "subject": "x"},
           {"ts": iso(1), "source": "email", "level": "info", "ok": True, "subject": "3 items on your list are on sale (11/12)"},
           {"ts": iso(1), "source": "email", "level": "error", "ok": False, "kind": "weekly", "subject": "x"},
           {"ts": iso(2), "source": "email", "level": "info", "ok": True, "subject": "Reminder: verify your Cartwise email"},
           {"ts": iso(2), "source": "email", "level": "info", "ok": True, "kind": "admin_report", "subject": "r"},
           {"ts": iso(2), "source": "email", "level": "info", "action": "resend-weekly-triggered"},
           {"ts": iso(3), "source": "api", "level": "error"}, {"ts": iso(10), "source": "api", "level": "error"},
           {"ts": iso(20), "source": "email", "level": "info", "ok": True, "kind": "weekly"}]
    scrapes = [{"job_id": "1", "started_at": iso(1), "stores_examined": 3, "total_deals": 900, "emails_sent": 2, "errors": []},
               {"job_id": "2", "started_at": iso(8), "stores_examined": 3, "errors": ["boom"]}]
    feedback = [{"ts": iso(2), "status": "new"}, {"ts": iso(30), "status": "done"}]
    rows = [
        {"pk": "em#1", "kind": "weekly", "to": "a@x.com", "sent_at": iso(1), "delivered_at": iso(1), "opened_at": iso(1), "opens": 5},
        {"pk": "em#2", "kind": "weekly", "to": "b@x.com", "sent_at": iso(1), "delivered_at": iso(1)},
        {"pk": "em#3", "kind": "reminder", "to": "f@x.com", "sent_at": iso(2), "bounced_at": iso(2), "bounce_type": "Permanent"},
        {"pk": "em#4", "kind": "admin_report", "to": "(admin)", "sent_at": iso(2), "delivered_at": iso(2)},
        {"pk": "em#5", "kind": "weekly", "to": "a@x.com", "sent_at": iso(9), "delivered_at": iso(9)},        # last week
        {"pk": "run#2026-11-18", "stores": 2, "users_with_list": 4, "users_matched": 3, "matches_total": 9,
         "emails_sent": 2, "skipped_unverified": 0, "skipped_opted_out": 1, "send_failed": 0},
        {"pk": "run#2026-11-11", "stores": 2, "users_with_list": 4, "users_matched": 2, "matches_total": 4, "emails_sent": 2},
        {"pk": "runitems#2026-11-18", "i:milk": 3, "i:eggs": 2, "expires_at": 1},
        {"pk": "snap#2026-11-12", "total": 6, "new": 1, "verified_rate": 80, "active_7d": 1},
        {"pk": "snap#2026-11-05", "total": 5, "new": 0, "verified_rate": 80, "active_7d": 2},
    ]
    return users, auth, app, scrapes, feedback, rows


class TestMetrics:
    @pytest.fixture
    def m(self, M):
        users, auth, app, scrapes, feedback, rows = sample()
        return M.build(NOW, users, auth, app, scrapes, feedback, rows, deleted_total=6)

    def test_growth_and_verified(self, m):
        g = m["growth"]
        assert (g["total"], g["new"], g["new_prev"], g["deleted_total"]) == (8, 2, 1, 6)      # __meta__ not a user
        assert (g["verified"], g["unverified"], g["verified_rate"]) == (6, 2, 75)

    def test_setup_funnel_is_cumulative(self, m):
        assert [s["count"] for s in m["funnel"]] == [8, 6, 6, 5, 4]
        assert [s["lost"] for s in m["funnel"]] == [0, 2, 0, 1, 1]
        assert m["funnel"][0]["step"] == "Signed up" and m["funnel"][-1]["pct"] == 50

    def test_verification_timing_and_reminders(self, m):
        v = m["verification"]
        assert v["cohort"] == 4                                    # d, e, f, h signed up in the last 30 days
        assert (v["within_24h"], v["within_7d"], v["later"], v["still_unverified"]) == (2, 0, 1, 1)
        assert v["within_24h_rate"] == 50
        assert [r["count"] for r in v["by_reminder"]] == [2, 1, 0, 0, 0] and v["tracked"] == 3
        assert v["unverified_over_28d"] == 1
        assert [r["count"] for r in v["reminders_pending"]] == [1, 0, 0, 0, 1]

    def test_activity(self, m):
        a = m["activity"]
        assert (a["active_7d"], a["active_30d"], a["dormant_60d"]) == (2, 3, 1)
        assert (a["sign_ins"], a["failed_sign_ins"], a["lockouts"]) == (2, 1, 1)             # window only
        assert (a["pin_resets_requested"], a["pin_resets_done"]) == (1, 1)

    def test_email_sent_by_kind(self, m):
        e = m["email"]
        kinds = {k["kind"]: (k["sent"], k["failed"]) for k in e["by_kind"]}
        assert kinds == {"weekly": (2, 1), "reminder": (1, 0)}     # old rows classified by subject; admin mail left out
        assert (e["sent"], e["failed"], e["unsubscribes"], e["unsubscribed_total"]) == (3, 1, 1, 1)

    def test_engagement_counts_unique_messages(self, m):
        g = m["engagement"]
        assert g["has_data"] and (g["sent"], g["delivered"], g["opened"], g["bounced"]) == (3, 2, 1, 1)
        assert g["delivered_rate"] == 67 and g["open_rate"] == 50                           # 5 opens of one mail = 1
        assert g["problems"] == [{"to": "f@x.com", "kind": "Verification reminder",
                                  "what": "Bounced (Permanent)", "when": iso(2)}]
        assert [k["kind"] for k in g["by_kind"]] == ["weekly", "reminder"]

    def test_admin_mail_is_tracked_on_its_own_line(self, m, M):
        ad = m["engagement"]["admin"]
        assert (ad["logged_sent"], ad["sent"], ad["delivered"], ad["delivered_rate"]) == (1, 1, 1, 100)
        assert m["engagement"]["sent"] == 3 and m["email"]["sent"] == 3          # totals unchanged
        # only an admin email has been tracked: tracking is on, but there is no user data yet
        only_admin = M.build(NOW, [user("a@x.com", 100, login=1)], [],
                             [{"ts": iso(1), "source": "email", "level": "info", "ok": True, "kind": "test"}],
                             [{"started_at": iso(1), "errors": []}], [],
                             [{"pk": "em#9", "kind": "admin_report", "sent_at": iso(0, 1), "delivered_at": iso(0, 1),
                               "opened_at": iso(0, 1)}])
        g = only_admin["engagement"]
        assert g["tracking_seen"] is True and g["has_data"] is False and g["opens_seen"] is True
        assert g["admin"]["open_rate"] == 100
        assert not any("Turn on the email events" in a["text"] for a in only_admin["attention"])

    def test_value_from_the_weekly_run(self, m):
        v = m["value"]
        assert v["last"]["date"] == "2026-11-18" and v["previous"]["date"] == "2026-11-11"
        assert (v["last"]["users_matched"], v["last"]["zero_match"], v["last"]["match_rate"]) == (3, 1, 75)
        assert v["last"]["avg_matches"] == 3.0
        assert v["top_items"] == [{"item": "milk", "count": 3}, {"item": "eggs", "count": 2}]

    def test_lists_and_health(self, m):
        assert m["lists"]["popular_items"][0] == {"item": "eggs", "count": 5} or m["lists"]["popular_items"][0]["count"] == 5
        assert (m["lists"]["with_list"], m["lists"]["empty_lists"], m["lists"]["stores"]) == (5, 3, 1)
        h = m["health"]
        assert (h["scrape_runs"], h["scrape_runs_with_errors"], h["errors"], h["errors_prev"]) == (1, 0, 2, 1)
        assert (h["feedback_new"], h["feedback_open"]) == (1, 1)

    def test_trend_and_last_week(self, m):
        assert [t["week"] for t in m["trend"]] == ["2026-11-05", "2026-11-12"]
        assert m["previous"]["week"] == "2026-11-12" and m["previous"]["total"] == 6

    def test_attention_lists_only_real_problems(self, m, M):
        texts = " | ".join(a["text"] for a in m["attention"])
        assert "failed to send" in texts and "bounced" in texts and "feedback" in texts and "28 days" in texts
        assert "No deal-fetch run" not in texts
        quiet = M.build(NOW, [user("a@x.com", 100, login=1)], [], [],
                        [{"started_at": iso(1), "errors": []}], [], [])
        assert quiet["attention"] == []

    def test_empty_site_does_not_divide_by_zero(self, M):
        m = M.build(NOW, [], [], [], [], [], [])
        assert m["growth"]["verified_rate"] is None and m["engagement"]["open_rate"] is None
        assert m["funnel"][0]["count"] == 0 and m["value"]["last"] is None

    def test_collect_reads_the_tables(self, M, dynamodb_tables):
        t = dynamodb_tables.Table("test-users")
        for u in sample()[0]:
            t.put_item(Item=u)
        m = M.collect(NOW)
        assert m["growth"]["total"] == 8 and m["growth"]["deleted_total"] == 6
        key = M.save_snapshot(m, NOW)
        assert key == "snap#2026-11-19"
        row = dynamodb_tables.Table("test-metrics").get_item(Key={"pk": key})["Item"]
        assert int(row["total"]) == 8 and int(row["verified_rate"]) == 75 and "expires_at" not in row


# ── Engagement webhook ────────────────────────────────────────────────────────

def _signed(body: dict, secret="whsec_" + base64.b64encode(b"s3cret").decode()):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b"s3cret", f"id1.{ts}.{raw}".encode(), hashlib.sha256).digest()).decode()
    return {"headers": {"svix-id": "id1", "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}, "body": raw,
            "requestContext": {"http": {"method": "POST", "path": "/inbound/email-list", "sourceIp": "1.1.1.1"}}}, secret


def _evt(kind, email_id="e1", to="ben@example.com", tags=None, **data):
    return {"type": kind, "created_at": "2026-11-19T10:00:00Z",
            "data": {"email_id": email_id, "to": [to], "subject": "3 items on your list are on sale",
                     "tags": {"kind": "weekly", "env": "prod"} if tags is None else tags, **data}}


class TestEngagementWebhook:
    @pytest.fixture
    def inbound(self, dynamodb_tables, monkeypatch):
        import inbound
        monkeypatch.setattr(inbound, "RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(b"s3cret").decode())
        dynamodb_tables.Table("test-users").put_item(Item={
            "email": "ben@example.com", "email_verified": True, "prefs": {"email_enabled": True, "items": ["milk"]}})
        return inbound

    def _row(self, ddb, email_id="e1"):
        return ddb.Table("test-metrics").get_item(Key={"pk": f"em#{email_id}"}).get("Item")

    def test_events_build_one_row_per_message(self, inbound, dynamodb_tables):
        for kind in ("email.sent", "email.delivered", "email.opened", "email.opened", "email.clicked"):
            ev, _ = _signed(_evt(kind))
            assert inbound.inbound_email_list(ev)["statusCode"] == 200
        row = self._row(dynamodb_tables)
        assert row["kind"] == "weekly" and row["to"] == "ben@example.com"
        assert row["sent_at"] and row["delivered_at"] and row["opened_at"] and row["clicked_at"]
        assert int(row["opens"]) == 2 and int(row["clicks"]) == 1 and int(row["expires_at"]) > time.time()

    def test_unsigned_events_are_rejected(self, inbound, dynamodb_tables):
        ev, _ = _signed(_evt("email.delivered"))
        ev["headers"]["svix-signature"] = "v1,AAAA"
        assert inbound.inbound_email_list(ev)["statusCode"] == 400
        assert self._row(dynamodb_tables) is None

    def test_other_environment_is_ignored(self, inbound, dynamodb_tables):
        ev, _ = _signed(_evt("email.delivered", tags={"kind": "weekly", "env": "staging"}))
        assert json.loads(inbound.inbound_email_list(ev)["body"])["skipped"] is True
        assert self._row(dynamodb_tables) is None

    def test_untagged_mail_is_classified_by_subject_and_kind_is_whitelisted(self, inbound, dynamodb_tables):
        ev, _ = _signed(_evt("email.delivered", tags={}))
        inbound.inbound_email_list(ev)
        assert self._row(dynamodb_tables)["kind"] == "weekly"
        ev, _ = _signed(_evt("email.delivered", email_id="e2", tags={"kind": "<script>", "env": "prod"}))
        inbound.inbound_email_list(ev)
        assert self._row(dynamodb_tables, "e2")["kind"] == "other"

    def test_spam_complaint_unsubscribes_the_account(self, inbound, dynamodb_tables):
        ev, _ = _signed(_evt("email.complained"))
        inbound.inbound_email_list(ev)
        u = dynamodb_tables.Table("test-users").get_item(Key={"email": "ben@example.com"})["Item"]
        assert u["prefs"]["email_enabled"] is False and u["complained_at"] and u["unsubscribed_at"]
        assert u["prefs"]["items"] == ["milk"]                                  # nothing else touched

    def test_permanent_bounce_flags_the_account_but_a_soft_one_does_not(self, inbound, dynamodb_tables):
        ev, _ = _signed(_evt("email.bounced", email_id="soft", bounce={"type": "Transient"}))
        inbound.inbound_email_list(ev)
        t = dynamodb_tables.Table("test-users")
        assert "email_bounced_at" not in t.get_item(Key={"email": "ben@example.com"})["Item"]
        ev, _ = _signed(_evt("email.bounced", email_id="hard", bounce={"type": "Permanent"}))
        inbound.inbound_email_list(ev)
        assert t.get_item(Key={"email": "ben@example.com"})["Item"]["email_bounced_at"]
        assert t.get_item(Key={"email": "ben@example.com"})["Item"]["prefs"]["email_enabled"] is True

    def test_list_import_still_works_through_the_same_webhook(self, inbound):
        ev, _ = _signed({"type": "something.else", "data": {}})
        assert json.loads(inbound.inbound_email_list(ev)["body"])["reason"] == "event type not email.received"


# ── New tracking on existing flows ────────────────────────────────────────────

class TestTracking:
    def test_verifying_records_when_and_after_how_many_reminders(self, dynamodb_tables, mail):
        import auth
        t = dynamodb_tables.Table("test-users")
        t.put_item(Item={"email": "n@x.com", "email_verified": False, "verify_token": "tok", "verify_reminders": 2,
                         "verify_token_exp": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        r = auth.verify_email({"token": "tok", "email": "n@x.com"})
        assert r["statusCode"] == 200
        u = t.get_item(Key={"email": "n@x.com"})["Item"]
        assert u["email_verified"] is True and u["verified_at"] and int(u["verified_after_reminders"]) == 2
        assert u["verified_via"] == "link"

    def test_every_outgoing_email_is_tagged_and_logged_by_kind(self, dynamodb_tables, mail, monkeypatch):
        import auth, prefs
        auth._send_verification_email("n@x.com", "tok")
        auth._send_pin_reset_email("n@x.com", "tok")
        auth._send_pin_changed_email("n@x.com")
        kinds = [{t["name"]: t["value"] for t in m["tags"]} for m in mail]
        assert [k["kind"] for k in kinds] == ["verify", "pin_reset", "pin_changed"]
        assert all(k["env"] == "prod" for k in kinds)
        logged = [i["kind"] for i in dynamodb_tables.Table("test-app-logs").scan()["Items"] if i.get("source") == "email"]
        assert sorted(logged) == ["pin_changed", "pin_reset", "verify"]

    def test_weekly_run_totals_add_up_across_stores(self, dynamodb_tables, mail, monkeypatch):
        monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
        spec = importlib.util.spec_from_file_location("cw_scraper_main912", os.path.join(_ROOT, "scraper", "main.py"))
        s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
        base = {"users_with_list": 2, "users_matched": 1, "matches_total": 3, "emails_sent": 1,
                "skipped_unverified": 0, "skipped_opted_out": 0, "send_failed": 0}
        s._record_run_summary("20261118-120000", base, {"milk": 1})
        s._record_run_summary("20261118-120000", {**base, "users_matched": 2, "matches_total": 5}, {"milk": 2, "eggs": 1})
        t = dynamodb_tables.Table("test-metrics")
        run = t.get_item(Key={"pk": "run#2026-11-18"})["Item"]
        assert (int(run["stores"]), int(run["users_with_list"]), int(run["users_matched"]), int(run["matches_total"])) == (2, 4, 3, 8)
        items = t.get_item(Key={"pk": "runitems#2026-11-18"})["Item"]
        assert int(items["i:milk"]) == 3 and int(items["i:eggs"]) == 1
        src = open(os.path.join(_ROOT, "scraper", "main.py"), encoding="utf-8").read()
        assert "if send_emails and job_id:" in src                    # weekly run only, not manual resends
        assert 'mailmeta.tags("weekly")' in src and 'mailmeta.tags("reminder")' in src


# ── Weekly report ─────────────────────────────────────────────────────────────

class TestWeeklyReport:
    @pytest.fixture
    def seeded(self, dynamodb_tables, mail):
        users, auth, app, scrapes, feedback, rows = sample()
        for tbl, items, key in (("test-users", users, None), ("test-metrics", rows, None)):
            for it in items:
                dynamodb_tables.Table(tbl).put_item(Item=it)
        for i, a in enumerate(app):
            dynamodb_tables.Table("test-app-logs").put_item(Item={"log_id": f"l{i}", **a})
        for s in scrapes:
            dynamodb_tables.Table("test-scrape-logs").put_item(Item=s)
        import report
        return report

    def test_scheduled_run_emails_the_admin_and_saves_the_snapshot(self, seeded, mail, dynamodb_tables):
        out = seeded.send_report(scheduled=True, now=NOW)
        assert out == {"sent": True, "snapshot": "snap#2026-11-19"}
        msg = mail[-1]
        assert msg["to"] == [ADMIN_TO]
        assert msg["subject"] == "Cartwise weekly report: 8 users, 2 new, 75% verified"
        assert {t["name"]: t["value"] for t in msg["tags"]}["kind"] == "admin_report"
        h = msg["html"]
        for needle in ("Weekly report", "Total users", "+2 vs last week", "Setup funnel", "Started a list",
                       "Needs attention", "Weekly deal alert", "Verification reminder", "Most matched items",
                       "/email-logo.png", "/#admin"):
            assert needle in h, needle
        assert ADMIN_TO not in h and ADMIN_TO not in msg["text"]      # the address is never in the content
        assert "Unsubscribe from Cartwise emails" not in h            # admin mail: no unsubscribe footer
        assert "Setup funnel:" in msg["text"] and "8" in msg["text"]
        assert dynamodb_tables.Table("test-metrics").get_item(Key={"pk": "snap#2026-11-19"}).get("Item")

    def test_send_now_does_not_write_a_snapshot(self, seeded, mail, dynamodb_tables):
        assert seeded.send_report(scheduled=False, now=NOW)["snapshot"] is None
        assert not dynamodb_tables.Table("test-metrics").get_item(Key={"pk": "snap#2026-11-19"}).get("Item")

    def test_no_admin_address_means_no_email(self, seeded, mail, monkeypatch):
        monkeypatch.setenv("FEEDBACK_TO", "")
        out = seeded.send_report(scheduled=True, now=NOW)
        assert out["sent"] is False and out["reason"] == "not_configured" and not mail

    def test_only_a_direct_invoke_triggers_the_scheduled_report(self, seeded, mail):
        import handler
        http = {"source": "admin-weekly-report", "headers": {}, "body": json.dumps({"source": "admin-weekly-report"}),
                "requestContext": {"http": {"method": "POST", "path": "/no-such-route", "sourceIp": "1.1.1.1"}}}
        handler.handler(http, None)
        assert not mail                                                # looked like the event, but came over HTTP
        out = handler.handler({"source": "admin-weekly-report"}, None)
        assert json.loads(out["body"])["sent"] is True and len(mail) == 1

    def test_sign_in_without_an_email_is_a_normal_failure(self, seeded, mail):
        import handler
        r = handler.handler({"headers": {}, "cookies": [], "body": json.dumps({"source": "admin-weekly-report"}),
                             "requestContext": {"http": {"method": "POST", "path": "/auth/login", "sourceIp": "1.1.1.1"}}}, None)
        assert r["statusCode"] == 401 and not mail                     # used to be an unhandled error

    def test_admin_endpoints_need_an_admin_session(self, seeded, mail):
        import handler
        for method, path in (("GET", "/admin/metrics"), ("POST", "/admin/report/send")):
            r = handler.handler({"headers": {}, "cookies": [], "body": "{}",
                                 "requestContext": {"http": {"method": method, "path": path, "sourceIp": "1.1.1.1"}}}, None)
            assert r["statusCode"] == 401, path
        assert not mail

    def test_deltas(self, seeded):
        d = seeded.delta
        assert d(8, 6) == "+2 vs last week" and d(5, 8) == "−3 vs last week" and d(4, 4) == "same as last week"
        assert d(75, 80, " pts") == "−5 pts vs last week" and d(5, None) == "" and d(None, 3) == ""
        assert seeded._day("2026-11-18") == "Nov 18"                  # a plain date is not shifted by time zone
        assert seeded._day("2026-11-19T03:00:00+00:00") == "Nov 18"   # 3 AM UTC is still the 18th in Eastern
        # Eastern time is worked out without a time zone database (none on Windows)
        loc = lambda s: seeded._local(datetime.fromisoformat(s)).strftime("%m-%d %H:%M")
        assert loc("2026-11-19T14:00:00+00:00") == "11-19 09:00"      # winter: UTC-5 (the report's send time)
        assert loc("2026-07-16T14:00:00+00:00") == "07-16 10:00"      # summer: UTC-4
        assert loc("2026-03-08T06:59:00+00:00") == "03-08 01:59" and loc("2026-03-08T07:00:00+00:00") == "03-08 03:00"
        assert loc("2026-11-01T05:59:00+00:00") == "11-01 01:59" and loc("2026-11-01T06:00:00+00:00") == "11-01 01:00"


class TestWiring:
    def test_deploy_creates_table_schedule_and_env(self):
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert 'create_table "${APP_NAME}-metrics"' in d and 'enable_ttl "metrics"' in d
        assert "table/${APP_NAME}-metrics" in d and d.count('"METRICS_TABLE" "${APP_NAME}-metrics"') == 6
        assert 'cron(0 14 ? * THU *)' in d and "admin-weekly-report" in d and "eventbridge-admin-report-invoke" in d
        assert '[ "${ENV}" = "staging" ] && REPORT_STATE="DISABLED"' in d
        t = open(os.path.join(_ROOT, "teardown.sh"), encoding="utf-8").read()
        assert '"${APP_NAME}-metrics"' in t and '"${APP_NAME}-admin-report"' in t

    def test_old_stats_endpoint_is_gone(self):
        h = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert "/admin/stats" not in h and '"/admin/metrics"' in h and '"/admin/report/send"' in h


class TestReportsPage:
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()

    def test_page_uses_the_new_endpoint_and_send_button(self):
        assert "/admin/metrics" in self.js and "/admin/stats" not in self.js
        assert "/admin/report/send" in self.js and "'admin-send-report'" in self.js
        assert 'data-action="admin-send-report"' in self.html and 'id="rpt-send-msg"' in self.html

    def test_every_section_is_rendered(self):
        body = self.js[self.js.index("function rptRender(m){"):self.js.index("async function adminLoadStats(){")]
        for title in ("Twelve-week trend", "Setup funnel", "Verification", "Activity", "Site health", "Email",
                      "This week’s deals", "Lists and stores", "Sign-ups by month", "Users by location",
                      "Most popular list items", "Needs attention"):
            assert title in body, title

    def test_charts_are_single_measure_with_hover_and_text_alternative(self):
        chart = self.js[self.js.index("function rptChart("):self.js.index("function rptRender(m){")]
        assert 'role="img"' in chart and "aria-label" in chart and "data-tip" in chart and "<title>" in chart
        assert "rpt-hit" in chart and 'tabindex="0"' in chart          # keyboard reachable
        assert self.js.count("rptChart('") == 4                        # four small charts, one measure each

    def test_admin_row_and_accurate_tracking_note(self):
        assert "Admin emails" in self.js and "not in the totals" in self.js
        assert "!eng.tracking_seen ?" in self.js and "Tracking is working, but no email to a user" in self.js

    def test_untrusted_text_is_escaped(self):
        body = self.js[self.js.index("function rptRender(m){"):self.js.index("async function adminLoadStats(){")]
        for field in ("p.to", "p.kind", "p.what", "k.label", "a.text", "s.step"):
            assert f"escH({field})" in body, field
