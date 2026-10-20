"""tests/test_v984.py — autocomplete from the 52-week history; log fixes (v9.8.4)."""
import base64
import gzip
import json
import os
import time

import pytest

import history as H

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _event(qs=None, headers=None):
    return {"queryStringParameters": qs or {}, "cookies": ["session=tok"], "headers": headers or {}}


@pytest.fixture
def session(dynamodb_tables):
    dynamodb_tables.Table("test-sessions").put_item(Item={
        "token": "tok", "email": "ben@example.com", "expires_at": int(time.time()) + 3600})
    dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com", "prefs": {}})
    return dynamodb_tables


def _index():
    return H.build_index([
        {"week": "2026-10-12", "deals": [{"title": "Cheerios Cereal", "brand": "General Mills", "savings": "$3.99"},
                                         {"title": "Boar's Head Ham", "brand": "Boar's Head", "savings": "$8.99 lb"}]},
        {"week": "2026-05-04", "deals": [{"title": "CHEERIOS CEREAL", "brand": "general mills", "savings": "$3.49"},
                                         {"title": "Publix Greek Yogurt", "brand": "", "savings": "$3.79"}]},
    ])


class TestTerms:
    def test_titles_and_brands_once_each(self):
        terms = H.autocomplete_terms(_index())
        assert terms == ["Boar's Head", "Boar's Head Ham", "Cheerios Cereal", "General Mills", "Publix Greek Yogurt"]

    def test_brands_survive_the_saved_summary(self):
        back = H.deserialize_index(H.serialize_index(_index()))
        assert H.autocomplete_terms(back) == H.autocomplete_terms(_index())


class TestEndpoint:
    def test_store_history_is_used(self, session, monkeypatch):
        import deals
        monkeypatch.setattr(deals, "_history_index", lambda sid: _index())
        body = json.loads(deals.get_deal_corpus(_event({"store_id": "1546"}))["body"])
        assert body["source"] == "history" and body["count"] == 5 and "Cheerios Cereal" in body["titles"]

    def test_gzip_when_accepted(self, session, monkeypatch):
        import deals
        monkeypatch.setattr(deals, "_history_index", lambda sid: _index())
        r = deals.get_deal_corpus(_event({"store_id": "1546"}, {"Accept-Encoding": "gzip, br"}))
        assert r["isBase64Encoded"] and r["headers"]["Content-Encoding"] == "gzip"
        body = json.loads(gzip.decompress(base64.b64decode(r["body"])))
        assert body["count"] == 5
        assert r["headers"]["Cache-Control"].startswith("private")

    def test_no_history_falls_back_to_global(self, session, monkeypatch):
        import deals
        monkeypatch.setattr(deals, "_history_index", lambda sid: {"weeks": [], "titles": {}, "brands": []})
        session.Table("test-deal-corpus").put_item(Item={"corpus_id": "global", "terms": {"Milk"}})
        body = json.loads(deals.get_deal_corpus(_event({"store_id": "9999"}))["body"])
        assert body["source"] == "global" and body["titles"] == ["Milk"]

    def test_scraper_no_longer_writes_global_corpus(self):
        src = open(os.path.join(_ROOT, "scraper", "main.py"), encoding="utf-8").read()
        assert "update_corpus" not in src


class TestLogs:
    def test_api_errors_record_endpoint(self, dynamodb_tables):
        import helpers
        helpers.set_request_context("POST", "/user/resend-weekly")
        helpers.err("Please verify your email address first.", 403)
        rows = dynamodb_tables.Table("test-app-logs").scan()["Items"]
        row = [r for r in rows if r.get("source") == "api"][-1]
        assert row["method"] == "POST" and row["path"] == "/user/resend-weekly" and int(row["status"]) == 403

    def test_admin_log_renders_resend_and_endpoint(self):
        js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
        assert "l.action==='resend-weekly-triggered'" in js and "↻ resend queued" in js
        assert "(endpoint not recorded)" in js

    def test_handler_sets_request_context(self):
        src = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert "set_request_context(method, path)" in src
