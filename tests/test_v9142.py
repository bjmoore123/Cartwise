"""v9.14.2: more report numbers open their users, and Back/Forward works across admin views."""
import os, re
import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
from datetime import datetime, timezone, timedelta

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


JS = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()


@pytest.fixture
def S(dynamodb_tables):
    import segments
    return segments


def people():
    return sample() + [
        U("fresh24@x.com", days_ago=5, verified_at=iso(5, hours_ago=-2), last_login=iso(2)),
        U("fresh7@x.com", days_ago=10, verified_at=iso(6)),
        U("freshold@x.com", days_ago=12),                                          # verified, no time
        U("blank@x.com", prefs={"store_id": "1", "items": ["  ", ""], "email_enabled": True}),
    ]


class TestNewGroups:
    def test_tags(self, S):
        t = {u["email"]: set(S.tags(u, NOW)) for u in people() if u["email"] != "__meta__"}
        assert {"verified", "new_30", "verified_24h_30", "verified_7d_30", "signed_in_7", "signed_in_30"} <= t["fresh24@x.com"]
        assert "verified_7d_30" in t["fresh7@x.com"] and "verified_24h_30" not in t["fresh7@x.com"]
        assert "verified_unknown_30" in t["freshold@x.com"] and "verified_7d_30" not in t["freshold@x.com"]
        assert "signed_in_30" in t["site@x.com"] and "signed_in_7" not in t["site@x.com"]
        assert "never_signed_in" in t["both@x.com"] and "never_signed_in" not in t["gone@x.com"]
        assert {"reach_store", "reach_list", "with_list", "recipient_engaged_7", "recipient_engaged_30"} <= t["both@x.com"]
        assert "reach_store" in t["nolist@x.com"] and "reach_list" not in t["nolist@x.com"]
        assert {"empty_list", "stop_list"} <= t["blank@x.com"] and "with_list" not in t["blank@x.com"]   # blank items aren't a list
        assert "verified" not in t["new@x.com"]

    def test_report_fields_match_their_lists(self, dynamodb_tables, S):
        import metrics
        users = people()
        m = metrics.build(NOW, users, [], [], [{"started_at": iso(1), "errors": []}], [], [])
        seg = m["segments"]
        for k in S.CATALOG:
            assert seg[k] == sum(1 for u in users if k in S.tags(u, NOW)), k
        v, a = m["verification"], m["activity"]
        assert v["cohort"] == seg["new_30"]
        assert v["within_24h"] == seg["verified_24h_30"]
        assert v["within_24h"] + v["within_7d"] == seg["verified_7d_30"]
        assert v["verified_time_unknown"] == seg["verified_unknown_30"]
        assert (a["active_7d"], a["active_30d"], a["never_signed_in"]) == (seg["signed_in_7"], seg["signed_in_30"], seg["never_signed_in"])
        assert m["lists"]["with_list"] == seg["with_list"]
        f = [x["count"] for x in m["funnel"]]
        assert f == [m["growth"]["total"] if "total" in m.get("growth", {}) else f[0],
                     seg["verified"], seg["reach_store"], seg["reach_list"], seg["alert_recipient"]]
        assert all(f[i] >= f[i + 1] for i in range(len(f) - 1))

    def test_attention_items_point_somewhere(self, dynamodb_tables, S):
        import metrics
        m = metrics.build(NOW, people(), [], [], [{"started_at": iso(1), "errors": ["boom"]}], [], [])
        for it in m["attention"]:
            if "seg" in it:
                assert it["seg"] in S.CATALOG
            if "log" in it:
                assert it["log"] in ("scrape", "app:email", "app:all")


class TestPage:
    def test_every_linked_key_exists(self, S):
        keys = set(re.findall(r"\bL\('([a-z0-9_]+)'", JS))
        assert len(keys) >= 25
        assert keys <= set(S.CATALOG), keys - set(S.CATALOG)

    def test_link_kinds_are_handled(self):
        for action in ("admin-open-segment", "admin-open-filter", "admin-open-log", "admin-open-tab", "admin-timeline"):
            assert f"'{action}'" in JS or f'"{action}"' in JS, action
        for ftype in ("item", "loc", "month"):
            assert f"data-type=\"{ftype}\"" in JS or f"'{ftype}'" in JS, ftype

    def test_back_and_forward(self):
        assert "history.pushState" in JS and "history.replaceState" in JS
        assert "addEventListener('popstate'" in JS
        assert "function adminApplyState" in JS and "function adminGo" in JS
        # the history dialog closes through Back from the X, Escape and the backdrop
        assert "if(o.id==='admin-tl-ov') return adminCloseTimeline()" in JS
        assert "APP_VERSION = '9.14.2'" in JS
