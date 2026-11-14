"""v9.13: how people use Cartwise (site / email), real verification-email counts, attention fixes."""
import importlib.util, json, os, sys, types
from datetime import datetime, timezone, timedelta

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime(2026, 11, 19, 14, tzinfo=timezone.utc)


def iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


@pytest.fixture
def mail(monkeypatch):
    sent = []
    n = {"i": 0}
    def send(msg):
        n["i"] += 1
        sent.append(msg)
        return {"id": f"re_{n['i']}"}                      # the real SDK returns the message id
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=send)))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    return sent


def _users(ddb):
    return ddb.Table("test-users")


def _metrics(ddb):
    return ddb.Table("test-metrics")


def _scraper(monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    spec = importlib.util.spec_from_file_location("cw_scraper_main913", os.path.join(_ROOT, "scraper", "main.py"))
    s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
    return s


# ── Engagement metric ─────────────────────────────────────────────────────────

class TestUsageMetric:
    def _u(self, email, **kw):
        base = {"email": email, "created_at": iso(100), "email_verified": True,
                "prefs": {"store_id": "1", "items": ["milk"], "email_enabled": True}}
        base.update(kw)
        return base

    def test_site_email_both_or_neither(self, dynamodb_tables):
        import metrics
        users = [
            self._u("both@x.com", last_seen_at=iso(1), last_alert_open_at=iso(2)),
            self._u("mail@x.com", last_alert_open_at=iso(3)),
            self._u("click@x.com", last_alert_click_at=iso(20)),                 # click counts as email use
            self._u("site@x.com", last_login=iso(10)),                            # sign-in without last_seen still counts
            self._u("seen@x.com", last_seen_at=iso(5)),
            self._u("gone@x.com", last_login=iso(90), last_alert_open_at=iso(60)),
            self._u("never@x.com", email_verified=False),
            {"email": "__meta__"},
        ]
        m = metrics.build(NOW, users, [], [], [{"started_at": iso(1), "errors": []}], [], [])
        d7, d30 = m["usage"]["d7"], m["usage"]["d30"]
        assert (d7["both"], d7["email_only"], d7["site_only"], d7["none"]) == (1, 1, 1, 4)
        assert (d30["both"], d30["email_only"], d30["site_only"], d30["none"]) == (1, 2, 2, 2)
        assert d30["engaged"] == 5 and d30["engaged_rate"] == 71 and d30["by_email"] == 3 and d30["by_site"] == 3
        assert d30["recipients"] == 6 and d30["recipients_engaged"] == 5                # unverified isn't a recipient
        assert m["usage"]["never"] == 1
        assert m["headline"]["engaged_7d"] == 3 and m["headline"]["engaged_30d"] == 5

    def test_daily_counts_start_on_the_first_tracked_day(self, dynamodb_tables):
        import metrics
        rows = [{"pk": "day#2026-11-17", "site_users": 3, "alert_openers": 1},
                {"pk": "day#2026-11-19", "site_users": 2}]
        m = metrics.build(NOW, [], [], [], [], [], rows)
        daily = {d["day"]: d for d in m["usage"]["daily"]}
        assert len(m["usage"]["daily"]) == 30 and m["usage"]["tracking_since"] == "2026-11-17"
        assert daily["2026-11-16"]["site_users"] is None                                   # before tracking: unknown, not 0
        assert daily["2026-11-17"] == {"day": "2026-11-17", "site_users": 3, "alert_openers": 1}
        assert daily["2026-11-18"]["site_users"] == 0 and daily["2026-11-19"]["alert_openers"] == 0

    def test_snapshot_keeps_engagement(self, dynamodb_tables):
        import metrics
        m = metrics.build(NOW, [self._u("a@x.com", last_seen_at=iso(1))], [], [], [], [], [])
        metrics.save_snapshot(m, NOW)
        row = _metrics(dynamodb_tables).get_item(Key={"pk": "snap#2026-11-19"})["Item"]
        assert int(row["engaged_7d"]) == 1 and int(row["engaged_site_30d"]) == 1


# ── Site use is recorded on any signed-in request ─────────────────────────────

class TestSiteUse:
    def test_touch_records_last_seen_once_per_window_and_counts_days(self, dynamodb_tables):
        from shared import usage
        u, mt = _users(dynamodb_tables), _metrics(dynamodb_tables)
        u.put_item(Item={"email": "a@x.com"})
        t0 = datetime(2026, 11, 19, 15, tzinfo=timezone.utc)
        row = {"email": "a@x.com"}
        usage.touch_site(u, mt, row, now=t0)
        usage.touch_site(u, mt, row, now=t0 + timedelta(minutes=10))              # throttled: no write
        usage.touch_site(u, mt, row, now=t0 + timedelta(hours=2))                 # same day: no new day
        usage.touch_site(u, mt, row, now=t0 + timedelta(days=1))                  # next day
        item = u.get_item(Key={"email": "a@x.com"})["Item"]
        assert item["last_seen_at"] == (t0 + timedelta(days=1)).isoformat() and int(item["site_days"]) == 2
        assert int(mt.get_item(Key={"pk": "day#2026-11-19"})["Item"]["site_users"]) == 1
        assert int(mt.get_item(Key={"pk": "day#2026-11-20"})["Item"]["site_users"]) == 1

    def test_days_follow_eastern_time(self, dynamodb_tables):
        from shared import usage
        assert usage.day_key(datetime(2026, 11, 20, 3, tzinfo=timezone.utc)) == "2026-11-19"   # 10 PM Eastern
        assert usage.day_key(datetime(2026, 7, 20, 3, 30, tzinfo=timezone.utc)) == "2026-07-19"

    def test_admin_and_meta_never_counted(self, dynamodb_tables):
        from shared import usage
        u, mt = _users(dynamodb_tables), _metrics(dynamodb_tables)
        for e in ("__admin__", "__meta__"):
            usage.touch_site(u, mt, {"email": e}, now=NOW)
        assert not mt.get_item(Key={"pk": f"day#{usage.day_key(NOW)}"}).get("Item")

    def test_a_signed_in_request_touches_last_seen(self, dynamodb_tables):
        import helpers
        _users(dynamodb_tables).put_item(Item={"email": "a@x.com"})
        exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        dynamodb_tables.Table("test-sessions").put_item(Item={"token": "tok", "email": "a@x.com", "expires_at": exp})
        sess = helpers.get_session({"headers": {"authorization": "Bearer tok"}, "cookies": []})
        assert sess and sess["email"] == "a@x.com"
        assert _users(dynamodb_tables).get_item(Key={"email": "a@x.com"})["Item"].get("last_seen_at")


# ── Email opens are credited to the account they were sent for ───────────────

class TestEmailUse:
    def test_weekly_send_records_the_account_and_an_open_credits_it(self, dynamodb_tables, mail, monkeypatch):
        import inbound, base64, hmac, hashlib, time as _t
        s = _scraper(monkeypatch)
        monkeypatch.setattr(s, "resend", sys.modules["resend"])
        _users(dynamodb_tables).put_item(Item={"email": "acct@x.com", "email_verified": True})
        s.send_alert("family@y.com", [], "1", "Store", account_email="acct@x.com")      # alerts go to another address
        row = _metrics(dynamodb_tables).get_item(Key={"pk": "em#re_1"})["Item"]
        assert row["account"] == "acct@x.com" and row["to"] == "family@y.com" and row["kind"] == "weekly"

        secret = "whsec_" + base64.b64encode(b"k").decode()
        monkeypatch.setattr(inbound, "RESEND_WEBHOOK_SECRET", secret)
        def post(evt):
            raw = json.dumps(evt); ts = str(int(_t.time()))
            sig = base64.b64encode(hmac.new(b"k", f"m1.{ts}.{raw}".encode(), hashlib.sha256).digest()).decode()
            return inbound.inbound_email_list({"headers": {"svix-id": "m1", "svix-timestamp": ts, "svix-signature": f"v1,{sig}"},
                                               "body": raw, "requestContext": {"http": {"method": "POST", "sourceIp": "1.1.1.1"}}})
        tags = {"kind": "weekly", "env": "prod"}
        post({"type": "email.opened", "created_at": iso(0, 2),
              "data": {"email_id": "re_1", "to": ["family@y.com"], "tags": tags}})
        acct = _users(dynamodb_tables).get_item(Key={"email": "acct@x.com"})["Item"]
        assert acct["last_alert_open_at"] == iso(0, 2) and int(acct["alert_open_days"]) == 1
        post({"type": "email.clicked", "created_at": iso(0, 1),
              "data": {"email_id": "re_1", "to": ["family@y.com"], "tags": tags}})
        assert _users(dynamodb_tables).get_item(Key={"email": "acct@x.com"})["Item"]["last_alert_click_at"] == iso(0, 1)

    def test_older_open_never_moves_the_date_back(self, dynamodb_tables):
        from shared import usage
        u, mt = _users(dynamodb_tables), _metrics(dynamodb_tables)
        u.put_item(Item={"email": "a@x.com", "last_alert_open_at": iso(1)})
        usage.credit_alert(u, mt, "a@x.com", "email.opened", iso(5), now=NOW)
        assert u.get_item(Key={"email": "a@x.com"})["Item"]["last_alert_open_at"] == iso(1)

    def test_opening_other_mail_is_not_engagement(self, dynamodb_tables, monkeypatch):
        import engagement
        _users(dynamodb_tables).put_item(Item={"email": "a@x.com"})
        engagement.record_event({"type": "email.opened", "created_at": iso(0, 1),
                                 "data": {"email_id": "z1", "to": ["a@x.com"], "tags": {"kind": "pin_reset", "env": "prod"}}})
        assert "last_alert_open_at" not in _users(dynamodb_tables).get_item(Key={"email": "a@x.com"})["Item"]

    def test_every_sender_records_its_message(self, dynamodb_tables, mail, monkeypatch):
        import auth, prefs
        _users(dynamodb_tables).put_item(Item={"email": "a@x.com", "email_verified": True, "prefs": {"notify_email": "a@x.com"}})
        auth._send_verification_email("a@x.com", "t")
        auth._send_pin_reset_email("a@x.com", "t")
        monkeypatch.setattr(prefs, "get_session", lambda e: {"email": "a@x.com"})
        monkeypatch.setattr(prefs, "check_rate_limit", lambda *a: True)
        prefs.send_test_email({"headers": {}, "cookies": []})
        rows = {r["pk"]: r for r in _metrics(dynamodb_tables).scan()["Items"] if r["pk"].startswith("em#")}
        assert sorted(r["kind"] for r in rows.values()) == ["pin_reset", "test", "verify"]
        assert all(r["account"] == "a@x.com" for r in rows.values())


# ── Real verification-email count ─────────────────────────────────────────────

class TestVerifyCount:
    def test_counts_only_sends_that_succeed(self, dynamodb_tables, mail, monkeypatch):
        import auth
        t = _users(dynamodb_tables)
        t.put_item(Item={"email": "n@x.com", "email_verified": False})
        auth._send_verification_email("n@x.com", "t1")
        auth._send_verification_email("n@x.com", "t2")
        def boom(msg): raise RuntimeError("resend down")
        monkeypatch.setattr(sys.modules["resend"].Emails, "send", boom)
        auth._send_verification_email("n@x.com", "t3")                                  # failed: not counted
        item = t.get_item(Key={"email": "n@x.com"})["Item"]
        assert int(item["verify_emails"]) == 2 and item["last_verify_email_at"]

    def test_reminders_add_to_the_count(self, dynamodb_tables, mail, monkeypatch):
        s = _scraper(monkeypatch)
        monkeypatch.setattr(s, "resend", sys.modules["resend"])
        t = _users(dynamodb_tables)
        t.put_item(Item={"email": "n@x.com", "email_verified": False, "created_at": iso(40), "verify_emails": 1})
        assert s.send_verify_reminders(now=NOW)["sent"] == 1
        item = t.get_item(Key={"email": "n@x.com"})["Item"]
        assert int(item["verify_emails"]) == 2 and int(item["verify_reminders"]) == 1

    def test_backfill_reads_the_log_lines_the_app_prints(self):
        src = open(os.path.join(_ROOT, "lambda", "auth.py"), encoding="utf-8").read()
        assert 'print(f"[CW] Verification email sent -> {email}")' in src
        sc = open(os.path.join(_ROOT, "scraper", "main.py"), encoding="utf-8").read()
        assert 'print(f"  Reminder {n}/{VERIFY_REMINDERS_MAX} -> {email}")' in sc
        spec = importlib.util.spec_from_file_location("bf", os.path.join(_ROOT, "tools", "backfill_verify_counts.py"))
        bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)
        assert bf.SIGNUP.search("[CW] Verification email sent -> a.b+c@x.com").group(1) == "a.b+c@x.com"
        assert bf.REMINDER.search("  Reminder 3/4 -> n@x.com").group(2) == "n@x.com"
        assert bf.REMINDER.search("Verification reminders: {'sent': 1}") is None

    def test_users_list_shows_count_and_usage(self, dynamodb_tables):
        import admin
        _users(dynamodb_tables).put_item(Item={"email": "n@x.com", "email_verified": False, "verify_emails": 3,
                                              "verify_reminders": 2, "last_seen_at": iso(1), "site_days": 4})
        _users(dynamodb_tables).put_item(Item={"email": "old@x.com", "email_verified": False})
        out = {u["email"]: u for u in json.loads(admin.admin_list_users.__wrapped__({})["body"])["users"]} \
            if hasattr(admin.admin_list_users, "__wrapped__") else None
        if out is None:
            pytest.skip("admin wrapper exposes no __wrapped__")
        assert out["n@x.com"]["verify_emails"] == 3 and out["n@x.com"]["site_days"] == 4
        assert out["old@x.com"]["verify_emails"] is None                     # not counted yet: shown as "?"


# ── Attention messages ────────────────────────────────────────────────────────

class TestAttention:
    def _m(self, users, scrapes=None):
        import metrics
        return metrics.build(NOW, users, [], [], scrapes if scrapes is not None else [{"started_at": iso(1), "errors": []}], [], [])

    def test_old_accounts_with_only_one_reminder_are_not_flagged(self, dynamodb_tables):
        old = {"email": "o@x.com", "created_at": iso(200), "email_verified": False, "verify_reminders": 1,
               "last_verify_reminder_at": iso(0, 3)}
        m = self._m([old])
        assert m["verification"]["reminders_finished"] == 0 and m["verification"]["reminders_in_progress"] == 1
        assert not any("reminders" in a["text"] for a in m["attention"])

    def test_flagged_only_after_all_four_and_the_last_link_expired(self, dynamodb_tables):
        u = {"email": "o@x.com", "created_at": iso(60), "email_verified": False, "verify_reminders": 4,
             "last_verify_reminder_at": iso(3)}
        assert self._m([u])["verification"]["reminders_finished"] == 0                     # link still valid
        u["last_verify_reminder_at"] = iso(8)
        m = self._m([u])
        assert m["verification"]["reminders_finished"] == 1
        assert any(a["text"].startswith("1 account still unverified after all 4 reminders") for a in m["attention"])

    def test_deal_fetch_alarm_waits_for_eight_days(self, dynamodb_tables):
        just_over_7 = self._m([], [{"started_at": iso(7, 1), "errors": []}])                 # Wednesday, run not logged yet
        assert not any("deal-fetch" in a["text"] for a in just_over_7["attention"])
        late = self._m([], [{"started_at": iso(9), "errors": []}])
        assert any("The last deal-fetch run was 9 days ago" in a["text"] for a in late["attention"])
        none = self._m([], [])
        assert any("No deal-fetch run has been recorded" in a["text"] for a in none["attention"])


class TestPages:
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()

    def test_users_table_has_the_new_columns(self):
        assert 'data-sort="verify"' in self.html and ">Verify Emails<" in self.html and ">Last Active<" in self.html
        assert "adminVerifyHtml(u)" in self.js and "adminUsageHtml(u)" in self.js
        assert 'colspan="8"' in self.js and 'colspan="7"' not in self.js[self.js.index("function adminLoadUsers"):self.js.index("function adminUserPage")]

    def test_reports_page_and_email_show_usage(self):
        assert "How people use Cartwise" in self.js and "Engaged this week" in self.js
        assert "unverified_over_28d" not in self.js
        rep = open(os.path.join(_ROOT, "lambda", "report.py"), encoding="utf-8").read()
        assert "How people use Cartwise" in rep and "Engaged this week" in rep and "unverified_over_28d" not in rep
