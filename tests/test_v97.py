"""tests/test_v97.py — v9.6 (data accuracy, ops) and v9.7 (inbound import) fixes."""
import importlib.util
import io
import json
import os
import sys
import time
import types
import urllib.error
from datetime import date

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture
def scraper_main(dynamodb_tables, monkeypatch):
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=None))
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    sys.modules.pop("scraper", None)
    spec = importlib.util.spec_from_file_location("cw_scraper_main97", os.path.join(_ROOT, "scraper", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("scraper", None)


# ── v9.6: ad week ─────────────────────────────────────────────────────────────

class TestWeekStart:
    def _deals(self, vf, n=5, st="WeeklyAd"):
        return [{"saving_type": st, "valid_from": vf}] * n

    def test_publix_md_format_on_a_monday(self, scraper_main):
        # Manual scrape on Monday 10/12 while the 10/7 ad is live: must file under 10/5, not 10/12
        deals = self._deals("1/1", 20, "Tpr") + self._deals("10/7")
        assert scraper_main._week_start(deals, today=date(2026, 10, 12)) == "2026-10-05"

    def test_new_ad_on_wednesday(self, scraper_main):
        assert scraper_main._week_start(self._deals("10/14"), today=date(2026, 10, 14)) == "2026-10-12"

    def test_year_rollover(self, scraper_main):
        assert scraper_main._week_start(self._deals("12/31"), today=date(2027, 1, 2)) == "2026-12-28"
        assert scraper_main._week_start(self._deals("1/1/2027"), today=date(2026, 12, 31)) == "2026-12-28"

    def test_majority_wins_and_placeholders_ignored(self, scraper_main):
        deals = self._deals("10/7", 30) + self._deals("9/30", 2) + self._deals("1/1", 50, "DigitalCoupon")
        assert scraper_main._week_start(deals, today=date(2026, 10, 9)) == "2026-10-05"

    def test_fallback_is_this_monday(self, scraper_main):
        assert scraper_main._week_start([], today=date(2026, 10, 15)) == "2026-10-12"


# ── v9.6: history audit tool ──────────────────────────────────────────────────

@pytest.fixture
def hist(dynamodb_tables):
    ddb = dynamodb_tables
    ddb.Table("test-deal-history").delete()
    t = ddb.create_table(
        TableName="test-deal-history",
        KeySchema=[{"AttributeName": "store_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "store_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")

    def week(wk, deals):
        chunks = [deals[i:i + 200] for i in range(0, max(len(deals), 1), 200)]
        for i, c in enumerate(chunks):
            t.put_item(Item={"store_id": "1546", "sk": f"{wk}#{i}", "deals": json.dumps(c), "count": len(c)})
        t.put_item(Item={"store_id": "1546", "sk": wk, "week": wk, "count": len(deals), "num_chunks": len(chunks)})
    normal = [{"id": f"d{i}", "title": f"Deal {i}", "savings": "$1"} for i in range(300)]
    for wk in ("2026-03-09", "2026-03-16", "2026-03-23", "2026-04-06"):
        week(wk, normal)
    week("2026-03-30", normal + normal)                   # written twice
    t.put_item(Item={"store_id": "1546", "sk": "_summary", "num_chunks": 0})
    return t


def _tool():
    spec = importlib.util.spec_from_file_location("history_audit", os.path.join(_ROOT, "tools", "history_audit.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHistoryAudit:
    def test_overview_flags_the_doubled_week(self, hist):
        out = []
        assert _tool().main(["--store", "1546"], tbl=hist, out=out.append) == 0
        line = next(l for l in out if l.startswith("2026-03-30"))
        assert "OUTLIER 2.0x" in line and "300 duplicate ids" in line
        assert not any("OUTLIER" in l for l in out if l.startswith("2026-03-23"))

    def test_dry_run_changes_nothing(self, hist):
        out = []
        _tool().main(["--store", "1546", "--week", "2026-03-30", "--dedupe"], tbl=hist, out=out.append)
        assert any("Dry run" in l for l in out)
        assert int(hist.get_item(Key={"store_id": "1546", "sk": "2026-03-30"})["Item"]["count"]) == 600

    def test_dedupe_with_backup(self, hist, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = []
        _tool().main(["--store", "1546", "--week", "2026-03-30", "--dedupe", "--yes"], tbl=hist, out=out.append)
        assert any("600 -> 300" in l for l in out)
        assert list(tmp_path.glob("history-backup-1546-2026-03-30-*.json"))
        assert int(hist.get_item(Key={"store_id": "1546", "sk": "2026-03-30"})["Item"]["count"]) == 300
        assert "Item" not in hist.get_item(Key={"store_id": "1546", "sk": "2026-03-30#2"})   # extra chunk gone
        assert "Item" not in hist.get_item(Key={"store_id": "1546", "sk": "_summary"})       # rebuilds
        assert int(hist.get_item(Key={"store_id": "1546", "sk": "2026-03-23"})["Item"]["count"]) == 300


# ── v9.7: inbound import ──────────────────────────────────────────────────────

LIST_HTML = ("<table><tr><th>#</th><th>Name</th><th>Quantity</th></tr>"
             "<tr><td>1</td><td>Milk</td><td>1</td></tr><tr><td>2</td><td>Eggs</td><td>2</td></tr>"
             "<tr><td>3</td><td>Bread</td><td>1</td></tr></table>")


@pytest.fixture
def inbound(dynamodb_tables, monkeypatch):
    import inbound as IB
    monkeypatch.setattr(IB, "_verify_resend_webhook", lambda e: True)
    monkeypatch.setattr(IB, "INBOUND_EMAIL_ADDR", "")
    monkeypatch.setenv("RESEND_API_KEY", "re_send")
    monkeypatch.delenv("RESEND_RECEIVING_KEY", raising=False)
    IB._rate_tbl = dynamodb_tables.Table("test-app-logs")
    dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com", "prefs": {"items": ["eggs"]}})
    return IB


def _hook(sender="Ben <ben@example.com>", subject="My List from Publix"):
    body = {"type": "email.received", "data": {"email_id": "em1", "to": ["x@in.resend.app"],
                                               "from": sender, "subject": subject}}
    return {"body": json.dumps(body), "headers": {}}


def _logs(ddb, action):
    return [i for i in ddb.Table("test-app-logs").scan()["Items"] if i.get("action") == action]


class TestInbound:
    def test_uses_receiving_key_and_logs_actually_added_items(self, inbound, dynamodb_tables, monkeypatch):
        seen = {}

        class _R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            seen["auth"] = req.headers.get("Authorization")
            return _R(json.dumps({"html": LIST_HTML}).encode())
        monkeypatch.setenv("RESEND_RECEIVING_KEY", "re_full")
        monkeypatch.setattr(inbound.urllib.request, "urlopen", fake_urlopen)
        r = inbound.inbound_email_list(_hook())
        assert json.loads(r["body"])["imported"] == 2
        assert seen["auth"] == "Bearer re_full"
        log = _logs(dynamodb_tables, "inbound-list-imported")[0]
        assert log["added_items"] == "['Milk', 'Bread']"            # not the first two parsed (Milk, Eggs)

    def test_403_with_sending_key_explains_the_fix(self, inbound, dynamodb_tables, monkeypatch):
        def deny(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"restricted"}'))
        monkeypatch.setattr(inbound.urllib.request, "urlopen", deny)
        assert inbound.inbound_email_list(_hook())["statusCode"] == 502
        log = _logs(dynamodb_tables, "inbound-fetch-failed")[0]
        assert log["key_kind"] == "sending" and "/publix/resend-receiving-key" in log["hint"]

    def test_unknown_sender_is_logged(self, inbound, dynamodb_tables):
        inbound.inbound_email_list(_hook(sender="stranger@example.com"))
        log = _logs(dynamodb_tables, "inbound-no-user")[0]
        assert log["sender"] == "stranger@example.com" and log["email_id"] == "em1"

    def test_rejections_carry_email_id(self, inbound, dynamodb_tables):
        inbound.inbound_email_list(_hook(subject="hello"))
        assert _logs(dynamodb_tables, "inbound-subject-rejected")[0]["email_id"] == "em1"


# ── v9.7: /user/matches reports the threshold used ────────────────────────────

def test_matches_reports_threshold(dynamodb_tables):
    import deals
    dynamodb_tables.Table("test-sessions").put_item(Item={"token": "tok", "email": "ben@example.com",
                                                         "expires_at": int(time.time()) + 3600})
    dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com",
                                                      "prefs": {"items": ["milk"], "store_id": "1546",
                                                                "matching": {"threshold": 72}}})
    dynamodb_tables.Table("test-deals").put_item(Item={"store_id": "1546", "num_chunks": 0,
                                                      "deals": json.dumps([{"id": "1", "title": "Whole Milk"}])})
    r = deals.get_matches({"queryStringParameters": {}, "cookies": ["session=tok"], "headers": {}})
    body = json.loads(r["body"])
    assert body["threshold"] == 72 and body["total"] == 1
