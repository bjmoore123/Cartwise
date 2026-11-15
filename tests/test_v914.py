"""v9.14: user groups behind every report number, complete per-user history, history gaps."""
import importlib.util, json, os, sys, types
from datetime import datetime, timezone, timedelta

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
NOW = datetime(2026, 11, 26, 14, tzinfo=timezone.utc)


def iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def U(email, days_ago=100, **kw):
    prefs = kw.pop("prefs", None) or {"store_id": "1", "items": ["milk"], "email_enabled": True}
    return {"email": email, "created_at": iso(days_ago), "email_verified": True, "prefs": prefs, **kw}


def sample():
    return [
        U("both@x.com", last_seen_at=iso(1), last_alert_open_at=iso(2)),
        U("mail@x.com", last_alert_open_at=iso(3)),
        U("site@x.com", last_login=iso(10)),
        U("gone@x.com", last_login=iso(90)),
        U("young@x.com", days_ago=20),                                                   # not seen, but too new to be dormant
        U("off@x.com", prefs={"store_id": "1", "items": ["x"], "email_enabled": False},
          alerts_off_at=iso(2), alerts_off_via="settings"),
        U("spam@x.com", prefs={"store_id": "1", "items": ["x"], "email_enabled": False},
          complained_at=iso(1), unsubscribed_at=iso(1), email_bounced_at=iso(1)),
        U("new@x.com", days_ago=3, email_verified=False, verify_emails=1,
          prefs={"store_id": "", "items": [], "email_enabled": True}),
        U("done@x.com", days_ago=50, email_verified=False, verify_reminders=4, last_verify_reminder_at=iso(9),
          verify_emails=0, prefs={"store_id": "", "items": [], "email_enabled": True}),
        U("nostore@x.com", prefs={"store_id": "", "items": [], "email_enabled": True}),
        U("nolist@x.com", prefs={"store_id": "2", "items": [], "email_enabled": True}),
        {"email": "__meta__", "deleted_count": 2},
    ]


@pytest.fixture
def S(dynamodb_tables):
    import segments
    return segments


class TestSegments:
    def test_tags(self, S):
        t = {u["email"]: set(S.tags(u, NOW)) for u in sample() if u["email"] != "__meta__"}
        assert {"use_both_7", "engaged_7", "use_both_30", "alert_recipient"} <= t["both@x.com"]
        assert {"use_email_7", "use_email_30"} <= t["mail@x.com"]
        assert {"use_none_7", "use_site_30"} <= t["site@x.com"]
        assert {"dormant_60", "use_none_30", "recipient_unengaged_30"} <= t["gone@x.com"]
        assert "dormant_60" not in t["young@x.com"]
        assert {"alerts_off", "unsub_7", "stop_alerts"} <= t["off@x.com"] and "alert_recipient" not in t["off@x.com"]
        assert {"spam", "spam_7", "bounced", "alerts_off", "unsub_7"} <= t["spam@x.com"]
        assert {"unverified", "unverified_new_30", "unverified_reminding", "new_7", "stop_verify"} <= t["new@x.com"]
        assert {"unverified_finished", "unverified_no_email"} <= t["done@x.com"] and "unverified_reminding" not in t["done@x.com"]
        assert {"stop_store", "no_store", "empty_list"} <= t["nostore@x.com"]
        assert {"stop_list", "empty_list"} <= t["nolist@x.com"] and "no_store" not in t["nolist@x.com"]

    def test_every_tag_is_in_the_catalog(self, S):
        for u in sample():
            assert set(S.tags(u, NOW)) <= set(S.CATALOG)
        assert {c["key"] for c in S.catalog_json()} == set(S.CATALOG)

    def test_report_numbers_equal_the_lists_they_open(self, dynamodb_tables, S):
        """The page shows m.segments for linked numbers; the report's own fields must agree too."""
        import metrics
        users = sample()
        m = metrics.build(NOW, users, [], [], [{"started_at": iso(1), "errors": []}], [], [])
        seg = m["segments"]
        listed = lambda k: sum(1 for u in users if k in S.tags(u, NOW))                  # noqa: E731
        for k in S.CATALOG:
            assert seg[k] == listed(k), k
        u7, u30 = m["usage"]["d7"], m["usage"]["d30"]
        assert (u7["both"], u7["email_only"], u7["site_only"], u7["none"], u7["engaged"]) == \
               (seg["use_both_7"], seg["use_email_7"], seg["use_site_7"], seg["use_none_7"], seg["engaged_7"])
        assert (u30["both"], u30["email_only"], u30["site_only"], u30["none"], u30["engaged"]) == \
               (seg["use_both_30"], seg["use_email_30"], seg["use_site_30"], seg["use_none_30"], seg["engaged_30"])
        assert u30["recipients"] == seg["alert_recipient"]
        v = m["verification"]
        assert (v["reminders_in_progress"], v["reminders_finished"], v["no_verify_email"], v["still_unverified"]) == \
               (seg["unverified_reminding"], seg["unverified_finished"], seg["unverified_no_email"], seg["unverified_new_30"])
        assert m["headline"]["new"] == seg["new_7"] and m["growth"]["unverified"] == seg["unverified"]
        assert [f["lost"] for f in m["funnel"][1:]] == [seg["stop_verify"], seg["stop_store"], seg["stop_list"], seg["stop_alerts"]]
        e = m["email"]
        assert (e["unsubscribes"], e["unsubscribed_total"], e["spam_complaints"], e["bounced_accounts"]) == \
               (seg["unsub_7"], seg["alerts_off"], seg["spam_7"], seg["bounced"])
        assert m["activity"]["dormant_60d"] == seg["dormant_60"]
        assert (m["lists"]["empty_lists"], m["lists"]["no_store"]) == (seg["empty_list"], seg["no_store"])

    def test_attention_items_link_to_their_list(self, dynamodb_tables):
        import metrics
        m = metrics.build(NOW, sample(), [], [], [{"started_at": iso(1), "errors": []}], [], [])
        segs = {a.get("seg") for a in m["attention"]}
        assert "unverified_finished" in segs

    def test_users_endpoint_returns_tags_and_catalog(self, dynamodb_tables):
        import admin
        t = dynamodb_tables.Table("test-users")
        for u in sample():
            t.put_item(Item=u)
        out = json.loads(admin.admin_list_users.__wrapped__({})["body"])
        by = {u["email"]: u for u in out["users"]}
        assert {"unverified", "unverified_no_email", "stop_verify"} <= set(by["done@x.com"]["segments"])   # date-independent tags
        assert by["off@x.com"]["alerts_off"] == iso(2) and by["off@x.com"]["alerts_off_via"] == "settings"
        assert {c["key"] for c in out["segment_catalog"]} >= {"use_email_30", "alerts_off"}


# ── New history, recorded going forward ───────────────────────────────────────

class TestRecording:
    def test_alerts_off_and_on_from_settings(self, dynamodb_tables, monkeypatch):
        import prefs
        t = dynamodb_tables.Table("test-users")
        t.put_item(Item={"email": "a@x.com", "prefs": {"email_enabled": True, "notify_email": "a@x.com"}})
        monkeypatch.setattr(prefs, "get_session", lambda e: {"email": "a@x.com"})
        prefs.save_prefs({}, {"prefs": {"email_enabled": False}})
        u = t.get_item(Key={"email": "a@x.com"})["Item"]
        assert u["alerts_off_via"] == "settings" and u["alerts_off_at"]
        prefs.save_prefs({}, {"prefs": {"email_enabled": True}})
        u = t.get_item(Key={"email": "a@x.com"})["Item"]
        assert u["alerts_on_via"] == "settings" and u["alerts_on_at"]
        before = u["alerts_on_at"]
        prefs.save_prefs({}, {"prefs": {"email_enabled": True, "items": ["milk"]}})        # no change: nothing recorded
        assert t.get_item(Key={"email": "a@x.com"})["Item"]["alerts_on_at"] == before

    def test_unsubscribe_link_admin_and_spam_record_how(self, dynamodb_tables):
        import prefs, admin, engagement
        from shared import usage
        t = dynamodb_tables.Table("test-users")
        for e in ("l@x.com", "a@x.com", "s@x.com"):
            t.put_item(Item={"email": e, "email_verified": True, "prefs": {"email_enabled": True}})
        import helpers
        tok = helpers._unsub_token("l@x.com")
        prefs.unsubscribe({"queryStringParameters": {"email": "l@x.com", "token": tok}, "headers": {}, "cookies": [],
                           "requestContext": {"http": {"method": "POST", "sourceIp": "1.1.1.1"}}})
        admin.admin_patch_prefs.__wrapped__({}, {"prefs": {"email_enabled": False}}, "a@x.com")
        engagement._on_complaint("s@x.com", "weekly")
        via = {e: t.get_item(Key={"email": e})["Item"].get("alerts_off_via") for e in ("l@x.com", "a@x.com", "s@x.com")}
        assert via == {"l@x.com": "unsubscribe link", "a@x.com": "admin", "s@x.com": "spam complaint"}
        assert usage.alerts_change(None, False, "nonsense")[1][":ao_v"] == "settings"

    def test_each_site_day_is_kept_on_the_account(self, dynamodb_tables):
        from shared import usage
        t, mt = dynamodb_tables.Table("test-users"), dynamodb_tables.Table("test-metrics")
        t.put_item(Item={"email": "a@x.com"})
        row = {"email": "a@x.com"}
        for d in (0, 0, 1, 3):
            usage.touch_site(t, mt, row, now=datetime(2026, 11, 20, 15, tzinfo=timezone.utc) + timedelta(days=d, minutes=40 * d))
        item = t.get_item(Key={"email": "a@x.com"})["Item"]
        assert item["site_day_list"] == ["2026-11-20", "2026-11-21", "2026-11-23"] and int(item["site_days"]) == 3

    def test_sign_in_events_are_kept_on_existing_accounts_only(self, dynamodb_tables):
        from logging_utils import _log_auth_event
        t = dynamodb_tables.Table("test-users")
        t.put_item(Item={"email": "a@x.com"})
        ev = {"headers": {"cloudfront-viewer-city": "Asheville"}, "requestContext": {"http": {"sourceIp": "1.1.1.1"}}}
        _log_auth_event(ev, "a@x.com", True)
        _log_auth_event(ev, "a@x.com", False, "bad_pin")
        _log_auth_event(ev, "a@x.com", False, "pin_reset_link_check_failed")              # not a person's action
        _log_auth_event(ev, "ghost@x.com", False, "bad_pin")                              # no account: not stored
        hist = t.get_item(Key={"email": "a@x.com"})["Item"]["auth_history"]
        assert [h.split("|")[1:] for h in hist] == [["sign_in", "Asheville"], ["bad_pin", "Asheville"]]
        assert "Item" not in t.get_item(Key={"email": "ghost@x.com"})

    def test_email_records_are_kept_three_years(self):
        from shared import mailmeta
        import engagement
        assert mailmeta.SEND_ROW_DAYS == 1095 and engagement.KEEP_DAYS == 1095


# ── The one-time history step ─────────────────────────────────────────────────

class TestMigration:
    @pytest.fixture
    def seeded(self, dynamodb_tables):
        t = dynamodb_tables.Table("test-users")
        t.put_item(Item=U("old@x.com", days_ago=200, last_login=iso(5), last_seen_at=iso(1),
                          prefs={"store_id": "1", "items": ["x"], "email_enabled": False}))
        t.put_item(Item=U("unsub@x.com", days_ago=200, unsubscribed_at=iso(2),
                          prefs={"store_id": "1", "items": ["x"], "email_enabled": False}))
        t.put_item(Item={"email": "legacy@x.com", "created_at": iso(300), "prefs": {"email_enabled": True}})
        t.put_item(Item=U("fresh@x.com", days_ago=0, email_verified=True, verified_at=iso(0)))
        t.put_item(Item={"email": "__meta__", "deleted_count": 1})
        a = dynamodb_tables.Table("test-auth-logs")
        a.put_item(Item={"log_id": "1", "ts": iso(60), "email": "old@x.com", "success": True, "reason": "", "city": "Arden"})
        a.put_item(Item={"log_id": "2", "ts": iso(59), "email": "old@x.com", "success": False, "reason": "bad_pin"})
        a.put_item(Item={"log_id": "3", "ts": iso(80), "email": "nobody@x.com", "success": False, "reason": "bad_pin"})
        l = dynamodb_tables.Table("test-app-logs")
        l.put_item(Item={"log_id": "e1", "ts": "2026-11-05T12:00:00+00:00", "source": "email", "level": "info", "ok": True,
                         "user": "old@x.com", "to": "old@x.com", "subject": "3 items on your list are on sale"})
        l.put_item(Item={"log_id": "e2", "ts": iso(1), "source": "email", "level": "info", "ok": True,
                         "user": "old@x.com", "to": "old@x.com", "kind": "weekly", "subject": "x"})     # after V913: em# has it
        import timeline
        return timeline

    def test_copies_logs_onto_accounts_and_records_gaps(self, seeded, dynamodb_tables):
        out = seeded.migrate(now=NOW)
        assert out["migrated"] == 4 and out["emails_copied"] == 1
        t = dynamodb_tables.Table("test-users")
        old = t.get_item(Key={"email": "old@x.com"})["Item"]
        assert old["history_since"] == NOW.isoformat()
        assert [h.split("|")[1] for h in old["auth_history"]] == ["sign_in", "bad_pin"]
        assert set(old["site_day_list"]) >= {iso(60)[:10], iso(5)[:10], iso(1)[:10]} and int(old["site_days"]) == len(old["site_day_list"])
        g = old["history_gaps"]
        assert {"sign_ins", "site_visits", "emails", "email_opens", "verified_at", "alerts_off"} <= set(g)
        assert "date wasn't recorded" in g["verified_at"] and "weren't recorded" in g["alerts_off"]
        legacy = t.get_item(Key={"email": "legacy@x.com"})["Item"]
        assert "predates email verification" in legacy["history_gaps"]["verified_at"]
        unsub = t.get_item(Key={"email": "unsub@x.com"})["Item"]
        assert unsub["alerts_off_via"] == "unsubscribe link" and unsub["alerts_off_at"] == iso(2)
        assert "alerts_off" not in unsub["history_gaps"]
        fresh = t.get_item(Key={"email": "fresh@x.com"})["Item"]
        assert fresh["history_gaps"] == {}                                                # complete from the start
        rows = [r for r in dynamodb_tables.Table("test-metrics").scan()["Items"] if r["pk"].startswith("emlog#")]
        assert len(rows) == 1 and rows[0]["account"] == "old@x.com" and rows[0]["kind"] == "weekly"

    def test_runs_once(self, seeded, dynamodb_tables):
        seeded.migrate(now=NOW)
        again = seeded.migrate(now=NOW + timedelta(days=1))
        assert again["migrated"] == 0
        old = dynamodb_tables.Table("test-users").get_item(Key={"email": "old@x.com"})["Item"]
        assert old["history_since"] == NOW.isoformat()                                   # never rewritten

    def test_deploy_runs_it_and_only_a_direct_invoke_can(self, seeded, dynamodb_tables):
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert '{"source":"history-migration"}' in d and "Account history preserved" in d
        import handler
        handler.handler({"source": "history-migration", "headers": {}, "body": "{}",
                         "requestContext": {"http": {"method": "POST", "path": "/nope", "sourceIp": "1.1.1.1"}}}, None)
        assert "history_since" not in dynamodb_tables.Table("test-users").get_item(Key={"email": "old@x.com"})["Item"]
        out = json.loads(handler.handler({"source": "history-migration"}, None)["body"])
        assert out["migrated"] == 4


# ── The timeline ──────────────────────────────────────────────────────────────

class TestTimeline:
    def test_builds_every_kind_of_event(self, dynamodb_tables):
        import timeline
        u = U("a@x.com", days_ago=30, verified_at=iso(29), verified_via="link", verified_after_reminders=1,
              site_day_list=["2026-11-20", "2026-11-24"], site_days=2,
              auth_history=["2026-11-20T15:00:00+00:00|sign_in|Asheville", "2026-11-21T15:00:00+00:00|bad_pin|",
                            "2026-11-21T15:05:00+00:00|pin_reset|"],
              alerts_off_at=iso(1), alerts_off_via="unsubscribe link", history_since=iso(25), history_gaps={},
              prefs={"store_id": "1", "store_name": "Publix", "items": ["a"], "email_enabled": False})
        rows = [{"pk": "em#1", "account": "a@x.com", "to": "a@x.com", "kind": "weekly", "sent_at": iso(5),
                 "delivered_at": iso(5), "opened_at": iso(4)},
                {"pk": "em#2", "to": "a@x.com", "kind": "weekly", "sent_at": iso(12), "bounced_at": iso(12), "bounce_type": "Permanent"},
                {"pk": "em#3", "account": "a@x.com", "to": "fam@y.com", "kind": "weekly", "sent_at": iso(19), "delivered_at": iso(19)},
                {"pk": "emlog#9", "account": "a@x.com", "kind": "verify", "sent_at": iso(30), "ok": True},
                {"pk": "em#4", "account": "(admin)", "kind": "admin_report", "sent_at": iso(1)}]
        fb = [{"ts": iso(3), "kind": "idea", "message": "Please add Aldi"}]
        out = timeline.build(u, rows, fb, [], NOW)
        titles = [e["title"] for e in out["events"]]
        assert titles.count("Used the site") == 2
        site = next(e for e in out["events"] if e["title"] == "Used the site" and e["ts"].startswith("2026-11-20"))
        assert site["detail"] == "Signed in from Asheville"
        assert "Wrong PIN entered" in titles and "Reset PIN with an emailed link" in titles
        assert "Verified email" in titles and "after 1 reminder" in next(e for e in out["events"] if e["title"] == "Verified email")["detail"]
        emails = [e for e in out["events"] if e["kind"] == "email"]
        assert any(e["detail"] == "delivered, opened" and e["tone"] == "good" for e in emails)
        assert any("bounced (Permanent)" in e["detail"] and e["tone"] == "bad" for e in emails)
        assert any("to fam@y.com" in e["detail"] for e in emails)
        assert not any("Admin report" in e["title"] for e in emails)
        off = next(e for e in out["events"] if e["title"] == "Turned off weekly alerts")
        assert off["detail"] == "via unsubscribe link"
        assert any(e["kind"] == "feedback" and "Aldi" in e["detail"] for e in out["events"])
        assert out["gaps"] == [] and out["summary"]["usage_30"] == "site" or out["summary"]["usage_30"] in ("both", "site", "email", "none")
        dated = [e["ts"] for e in out["events"] if e["dated"]]
        assert dated == sorted(dated, reverse=True)

    def test_gaps_and_undated_events_are_shown(self, dynamodb_tables):
        import timeline
        u = U("o@x.com", days_ago=200, history_since=iso(1),
              history_gaps={"verified_at": "Verified before Nov 14, 2026; the date wasn't recorded.",
                            "alerts_off": "Alerts were turned off before Nov 26, 2026; when and how weren't recorded."},
              verify_emails=3, prefs={"store_id": "", "items": [], "email_enabled": False})
        out = timeline.build(u, [], [], [], NOW)
        assert {g["what"] for g in out["gaps"]} == {"verified_at", "alerts_off"}
        undated = [e["title"] for e in out["events"] if not e["dated"]]
        assert "Verified email" in undated and "Turned off weekly alerts" in undated
        assert "3 earlier verification emails" in undated

    def test_account_not_yet_migrated_uses_the_sign_in_log(self, dynamodb_tables):
        import timeline
        u = U("n@x.com", days_ago=10)
        out = timeline.build(u, [], [], [{"ts": iso(2), "success": False, "reason": "locked_out"},
                                         {"ts": iso(3), "success": True, "reason": "", "city": "Arden"}], NOW)
        titles = [e["title"] for e in out["events"]]
        assert "Locked out (too many wrong PINs)" in titles and "Used the site" in titles

    def test_endpoint(self, dynamodb_tables):
        import admin, handler
        dynamodb_tables.Table("test-users").put_item(Item=U("a@x.com"))
        r = handler.handler({"headers": {}, "cookies": [], "body": "",
                             "requestContext": {"http": {"method": "GET", "path": "/admin/users/a%40x.com/timeline", "sourceIp": "1.1.1.1"}}}, None)
        assert r["statusCode"] == 401
        ok = admin.admin_user_timeline.__wrapped__({}, "a@x.com")
        assert ok["statusCode"] == 200 and json.loads(ok["body"])["email"] == "a@x.com"
        assert admin.admin_user_timeline.__wrapped__({}, "none@x.com")["statusCode"] == 404


class TestPages:
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()

    def test_report_numbers_link_to_groups(self):
        body = self.js[self.js.index("function rptRender(m){"):self.js.index("async function adminLoadStats(){")]
        for key in ("use_email_30", "use_site_7", "engaged_7", "new_7", "unverified_finished", "unverified_reminding",
                    "alerts_off", "unsub_7", "bounced", "spam_7", "dormant_60", "empty_list", "no_store",
                    "recipient_unengaged_30", "stop_verify"):
            assert f"'{key}'" in body, key
        assert 'data-action="admin-open-segment"' in body

    def test_users_tab_controls(self):
        for needle in ('id="admin-user-seg"', 'data-action="admin-users-csv"', 'id="admin-seg-banner"',
                       'id="admin-seg-th"', 'id="admin-tl-ov"'):
            assert needle in self.html, needle
        for fn in ("function adminOpenSegment(", "function adminUsersCsv(", "function adminShowTimeline(",
                   "function adminRenderTimeline(", "'admin-user-seg'"):
            assert fn in self.js, fn

    def test_timeline_escapes_everything_it_shows(self):
        body = self.js[self.js.index("function adminRenderTimeline(){"):self.js.index("function adminUserPage(dir){")]
        for f in ("escH(e.title)", "escH(e.detail)", "escH(g.note)", "escH(sm.store"):
            assert f in body, f
