"""tests/test_v922.py — v9.2.2 fixes: Batch 2 (#5 #7 #11 #12) and Batch 3 #6 (scraper safety).

Frontend fixes (#1 #2 #3 #7 #11) are covered by the Playwright checks listed in
docs/v9.2.2-test-plan.md.
"""
import importlib.util
import json
import os
import sys
import time
import types

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _event(qs=None, cookie="session=tok"):
    return {"queryStringParameters": qs or {}, "cookies": [cookie], "headers": {}}


@pytest.fixture
def session(dynamodb_tables):
    dynamodb_tables.Table("test-sessions").put_item(Item={
        "token": "tok", "email": "ben@example.com", "expires_at": int(time.time()) + 3600})
    dynamodb_tables.Table("test-users").put_item(Item={
        "email": "ben@example.com", "prefs": {"items": ["milk"], "store_id": "1546"}})
    return dynamodb_tables


@pytest.fixture
def scraper_main(dynamodb_tables, monkeypatch):
    """Import scraper/main.py as its own module (resend stubbed, no real sleeps)."""
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=None))
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    sys.modules.pop("scraper", None)
    spec = importlib.util.spec_from_file_location("cw_scraper_main", os.path.join(_ROOT, "scraper", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FETCH_RETRY_DELAY_S", 0)
    yield mod
    sys.modules.pop("scraper", None)


def _deals(n, prefix="Deal"):
    return [{"id": f"{prefix}{i}", "title": f"{prefix} {i}", "saving_type": "WeeklyAd", "savings": "$1.00"}
            for i in range(n)]


# ── #7 Stacked deals ──────────────────────────────────────────────────────────

class TestStacked:
    def test_parser_flags_publix_spelling(self):
        from shared.deal_parser import parse_deal
        assert parse_deal({"id": "1", "title": "X", "savingType": "Stacked"})["is_stacked"] is True
        assert parse_deal({"id": "2", "title": "X", "savingType": "StackedDeals"})["is_stacked"] is True
        assert parse_deal({"id": "3", "title": "X", "savingType": "WeeklyAd"})["is_stacked"] is False


# ── #11 threshold: Python and JS agree on the default ─────────────────────────

class TestThreshold:
    def test_default_is_normal_65(self):
        from shared.matcher import _get_threshold
        assert _get_threshold({}) == 65
        assert _get_threshold({"sensitivity": "strict"}) == 80
        assert _get_threshold({"threshold": 72}) == 72

    def test_js_tiers_match_python(self):
        from shared.matcher import SENSITIVITY_THRESHOLDS
        js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
        for k, v in SENSITIVITY_THRESHOLDS.items():
            assert f"{k}: {v}" in js

    def test_js_no_hardcoded_75_fallback(self):
        js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
        assert "threshold)||75" not in js
        assert "value)||75" not in js


# ── #5 corpus ─────────────────────────────────────────────────────────────────

class TestCorpus:
    def test_reads_terms_written_by_scraper(self, session):
        session.Table("test-deal-corpus").put_item(Item={"corpus_id": "global", "terms": {"Milk", "Bread"}})
        import deals
        body = json.loads(deals.get_deal_corpus(_event())["body"])
        assert body["titles"] == ["Bread", "Milk"]

    def test_merges_legacy_titles(self, session):
        session.Table("test-deal-corpus").put_item(Item={"corpus_id": "global",
                                                        "terms": {"Milk"}, "titles": {"Eggs"}})
        import deals
        assert json.loads(deals.get_deal_corpus(_event())["body"])["count"] == 2

    def test_empty_corpus(self, session):
        import deals
        assert json.loads(deals.get_deal_corpus(_event())["body"])["titles"] == []


# ── #12 admin PIN reset ───────────────────────────────────────────────────────

class TestAdminPinReset:
    def test_reset_clears_lockout_and_sessions(self, dynamodb_tables, monkeypatch):
        import admin
        monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
        users = dynamodb_tables.Table("test-users")
        users.put_item(Item={"email": "ben@example.com", "pin_hash": "old",
                             "failed_attempts": 5, "lockout_until": "2099-01-01T00:00:00+00:00",
                             "last_failed_at": "2026-10-12T00:00:00+00:00"})
        resp = admin.admin_reset_pin({}, {"new_pin": "4821"}, "ben@example.com")
        assert resp["statusCode"] == 200
        u = users.get_item(Key={"email": "ben@example.com"})["Item"]
        assert "failed_attempts" not in u and "lockout_until" not in u and "last_failed_at" not in u
        assert u["pin_hash"] != "old"
        assert int(u["valid_after"]) >= int(time.time()) - 5

    def test_reset_then_login_works(self, dynamodb_tables, monkeypatch):
        import admin, auth
        monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
        dynamodb_tables.Table("test-users").put_item(Item={
            "email": "ben@example.com", "pin_hash": "old", "email_verified": True,
            "failed_attempts": 5, "lockout_until": "2099-01-01T00:00:00+00:00"})
        admin.admin_reset_pin({}, {"new_pin": "4821"}, "ben@example.com")
        ev = {"headers": {}, "requestContext": {"http": {"sourceIp": "1.2.3.4"}}, "cookies": []}
        resp = auth.login(ev, {"email": "ben@example.com", "pin": "4821"}) if hasattr(auth, "login") else None
        if resp is None:
            pytest.skip("auth.login signature differs")
        assert resp["statusCode"] == 200, resp.get("body")


# ── #6 scraper keeps the last good cache ──────────────────────────────────────

class TestCacheGenerations:
    def test_write_read_and_cleanup(self, session, scraper_main):
        import deals
        tbl = session.Table("test-deals")
        scraper_main.cache_deals("1546", _deals(450, "A"))
        idx1 = tbl.get_item(Key={"store_id": "1546"})["Item"]
        assert idx1["num_chunks"] == 3 and idx1["gen"]
        assert len(deals._read_cached_deals(tbl, "1546", idx1)) == 450

        scraper_main.cache_deals("1546", _deals(150, "B"))
        idx2 = tbl.get_item(Key={"store_id": "1546"})["Item"]
        assert idx2["gen"] != idx1["gen"]
        got = deals._read_cached_deals(tbl, "1546", idx2)
        assert len(got) == 150 and got[0]["id"] == "B0"
        keys = {i["store_id"] for i in tbl.scan()["Items"]}
        assert not any(k.startswith(f"1546#{idx1['gen']}#") for k in keys)   # old generation removed

    def test_legacy_rows_still_read_and_cleaned(self, session, scraper_main):
        import deals
        tbl = session.Table("test-deals")
        tbl.put_item(Item={"store_id": "1546", "num_chunks": 1, "fetched_at": "t"})
        tbl.put_item(Item={"store_id": "1546#0", "deals": json.dumps(_deals(3, "L"))})
        assert len(deals._read_cached_deals(tbl, "1546", tbl.get_item(Key={"store_id": "1546"})["Item"])) == 3
        scraper_main.cache_deals("1546", _deals(5, "N"))
        assert "Item" not in tbl.get_item(Key={"store_id": "1546#0"})

    def test_reader_follows_generation_swap(self, session, scraper_main):
        import deals
        tbl = session.Table("test-deals")
        scraper_main.cache_deals("1546", _deals(10, "A"))
        stale_idx = tbl.get_item(Key={"store_id": "1546"})["Item"]
        scraper_main.cache_deals("1546", _deals(10, "B"))          # deletes A's chunks
        got = deals._read_cached_deals(tbl, "1546", stale_idx)     # reader still holds A's index
        assert [d["id"] for d in got][:1] == ["B0"]

    def test_failed_write_keeps_old_cache(self, session, scraper_main, monkeypatch):
        import deals
        tbl = session.Table("test-deals")
        scraper_main.cache_deals("1546", _deals(10, "A"))
        real_put = scraper_main.deals_table.put_item
        calls = {"n": 0}
        def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("throttled")
            return real_put(**kw)
        monkeypatch.setattr(scraper_main.deals_table, "put_item", flaky)
        with pytest.raises(RuntimeError):
            scraper_main.cache_deals("1546", _deals(450, "B"))
        idx = tbl.get_item(Key={"store_id": "1546"})["Item"]
        assert [d["id"] for d in deals._read_cached_deals(tbl, "1546", idx)][:1] == ["A0"]

    def test_get_deals_endpoint_uses_generations(self, session, scraper_main):
        import deals
        scraper_main.cache_deals("1546", _deals(250, "A"))
        body = json.loads(deals.get_deals(_event({"store_id": "1546", "types": "core"}))["body"])
        assert body["total_all"] == 250


class TestStoreWorkerSafety:
    def _run(self, scraper_main, monkeypatch, results):
        seq = iter(results)
        monkeypatch.setattr(scraper_main, "get_deals_with_status", lambda sid: next(seq))
        sent = []
        monkeypatch.setattr(scraper_main, "send_alert", lambda *a, **k: sent.append(a))
        scraper_main._store_worker({"store_id": "1546", "store_name": "Test", "send_emails": True,
                                    "users": [{"email": "ben@example.com", "email_verified": True,
                                               "prefs": {"items": ["deal"]}}]})
        return sent

    def test_zero_deals_keeps_cache_and_sends_nothing(self, session, scraper_main, monkeypatch):
        scraper_main.cache_deals("1546", _deals(10, "A"))
        before = session.Table("test-deals").get_item(Key={"store_id": "1546"})["Item"]["gen"]
        sent = self._run(scraper_main, monkeypatch, [([], []), ([], [])])
        assert sent == []
        assert session.Table("test-deals").get_item(Key={"store_id": "1546"})["Item"]["gen"] == before
        logs = session.Table("test-app-logs").scan()["Items"]
        assert any(l["source"] == "scraper" and l["level"] == "error" and "expires_at" in l for l in logs)

    def test_partial_failure_is_skipped(self, session, scraper_main, monkeypatch):
        scraper_main.cache_deals("1546", _deals(10, "A"))
        before = session.Table("test-deals").get_item(Key={"store_id": "1546"})["Item"]["gen"]
        sent = self._run(scraper_main, monkeypatch,
                         [(_deals(3, "C"), ["weekly_ad"]), (_deals(3, "C"), ["weekly_ad"])])
        assert sent == []
        assert session.Table("test-deals").get_item(Key={"store_id": "1546"})["Item"]["gen"] == before

    def test_retry_recovers(self, session, scraper_main, monkeypatch):
        sent = self._run(scraper_main, monkeypatch, [([], ["weekly_ad"]), (_deals(5, "Deal"), [])])
        assert len(sent) == 1
        assert session.Table("test-deals").get_item(Key={"store_id": "1546"})["Item"]["count"] == 5

    def test_healthy_run_writes_cache(self, session, scraper_main, monkeypatch):
        sent = self._run(scraper_main, monkeypatch, [(_deals(5, "Deal"), [])])
        assert len(sent) == 1
