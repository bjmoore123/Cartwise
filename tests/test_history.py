"""tests/test_history.py — v9.2 history summary (Batch 1) and per-env cookies (Batch 0)."""
import json
import os
import time

import boto3
import pytest
from moto import mock_aws

import history as H


def _snap(week, titles):
    return {"week": week, "deals": [{"title": t, "savings": f"${i + 1}.00"} for i, t in enumerate(titles)]}


WEEKS = [f"2026-{m:02d}-{d:02d}" for m, d in
         [(10, 5), (9, 28), (9, 21), (9, 14), (9, 7), (8, 31), (8, 24), (8, 17),
          (8, 10), (8, 3), (7, 27), (7, 20), (7, 13)]]  # newest first, 13 weeks


# ── Pure functions ────────────────────────────────────────────────────────────

class TestNormTitle:
    def test_matches_js_rule(self):
        assert H.norm_title("Ben & Jerry's Ice Cream, 16 oz") == "ben jerry s ice cream 16 oz"
        assert H.norm_title("  Publix   Milk ") == "publix milk"
        assert H.norm_title(None) == ""


class TestFuzzyScore:
    def test_substring_is_100(self):
        assert H.fuzzy_score("milk", "publix whole milk") == 100

    def test_partial_tokens(self):
        assert H.fuzzy_score("greek yogurt cups", "chobani greek yogurt") == 67
        assert H.fuzzy_score("chicken breast", "boneless chicken breasts") == 100

    def test_empty(self):
        assert H.fuzzy_score("", "x") == 0
        assert H.fuzzy_score("x", "") == 0


class TestBadges:
    def test_repeat_when_on_sale_last_week(self):
        idx = H.build_index([_snap(WEEKS[0], ["Chips"]), _snap(WEEKS[1], ["Chips"])])
        assert "chips" in H.compute_badges(idx)["repeat"]

    def test_rare_needs_12_weeks(self):
        snaps = [_snap(WEEKS[0], ["Saffron"])] + [_snap(w, ["Other"]) for w in WEEKS[1:]]
        idx = H.build_index(snaps)
        assert "saffron" in H.compute_badges(idx)["rare"]
        idx_short = H.build_index(snaps[:5])
        assert "saffron" not in H.compute_badges(idx_short)["rare"]

    def test_frequent_item_gets_no_badge(self):
        snaps = [_snap(WEEKS[0], ["Milk"])] + [_snap(w, ["Milk"] if i % 2 else ["Other"])
                                               for i, w in enumerate(WEEKS[1:])]
        badges = H.compute_badges(H.build_index(snaps))
        assert "milk" not in badges["repeat"] + badges["rare"]  # not last week (i=0 is "Other"), and on sale 6 past weeks

    def test_only_newest_week_titles(self):
        idx = H.build_index([_snap(WEEKS[0], ["A"]), _snap(WEEKS[1], ["B"]), _snap(WEEKS[2], ["B"])])
        b = H.compute_badges(idx)
        assert "b" not in b["repeat"] + b["rare"]

    def test_order_independent(self):
        a = H.build_index([_snap(WEEKS[1], ["Chips"]), _snap(WEEKS[0], ["Chips"])])
        assert a["weeks"][0] == WEEKS[0]
        assert "chips" in H.compute_badges(a)["repeat"]


class TestFrequency:
    def test_counts_past_weeks_only(self):
        snaps = [_snap(WEEKS[0], ["Publix Whole Milk"]),
                 _snap(WEEKS[1], ["Publix Whole Milk"]),
                 _snap(WEEKS[2], ["Organic Milk 2%"]),
                 _snap(WEEKS[3], ["Bread"])]
        freq = H.item_frequency(H.build_index(snaps), ["Milk", "bread", "saffron", {"name": "Bread"}])
        assert freq == {"milk": 2, "bread": 1, "saffron": 0}

    def test_one_week_counted_once(self):
        snaps = [_snap(WEEKS[0], []), _snap(WEEKS[1], ["Milk A", "Milk B"])]
        assert H.item_frequency(H.build_index(snaps), ["milk"]) == {"milk": 1}


class TestTitlePoints:
    def test_oldest_first(self):
        snaps = [_snap(WEEKS[0], ["Chips"]), _snap(WEEKS[2], ["Chips"]), _snap(WEEKS[1], ["Other"])]
        pts = H.title_points(H.build_index(snaps), "CHIPS!")
        assert [p["week"] for p in pts] == [WEEKS[2], WEEKS[0]]
        assert H.title_points(H.build_index(snaps), "nope") == []


class TestFastFrequencyAndPersistence:
    def test_fast_matches_reference(self):
        import random
        random.seed(7)
        words = ["milk", "whole", "publix", "greek", "yogurt", "chicken", "breast", "chips", "a", "&", "100%", "siggi's"]
        snaps = [_snap(w, [" ".join(random.sample(words, random.randint(1, 5))).title() for _ in range(40)])
                 for w in WEEKS[:6]]
        idx = H.build_index(snaps)
        items = [" ".join(random.sample(words, random.randint(1, 4))) for _ in range(60)] + ["a", "milk", "nothing here"]

        def ref(it):
            it = it.strip().lower()
            w = set()
            for t, wk in idx["lower_titles"].items():
                if H.fuzzy_score(it, t) >= 70:
                    w |= wk
            w.discard(0)
            return len(w)
        fast = H.item_frequency(idx, items)
        assert all(fast[i.strip().lower()] == ref(i) for i in items)

    def test_serialize_roundtrip_identical(self):
        snaps = [_snap(WEEKS[0], ["Chips", "CHIPS!", "Milk"]), _snap(WEEKS[1], ["Chips"])]
        idx = H.build_index(snaps)
        back = H.deserialize_index(H.serialize_index(idx))
        assert back["weeks"] == idx["weeks"] and back["titles"] == idx["titles"]
        assert back["lower_titles"] == idx["lower_titles"]


class TestScale:
    def test_30_weeks_2000_deals_is_fast(self):
        snaps = [_snap(f"2026-W{w:02d}", [f"Product {(w * 7 + i) % 3000} Brand {i % 50}" for i in range(2000)])
                 for w in range(30)]
        t = time.time()
        idx = H.build_index(snaps)
        H.compute_badges(idx)
        H.item_frequency(idx, [f"product {i}" for i in range(20)])
        assert time.time() - t < 5.0


# ── Endpoint (moto) ───────────────────────────────────────────────────────────

@pytest.fixture
def api_env(dynamodb_tables):
    ddb = dynamodb_tables
    ddb.Table("test-deal-history").delete()
    ddb.create_table(
        TableName="test-deal-history",
        KeySchema=[{"AttributeName": "store_id", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "store_id", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    hist = ddb.Table("test-deal-history")
    for week, titles in [(WEEKS[0], ["Chips", "Milk"]), (WEEKS[1], ["Chips"]), (WEEKS[2], ["Milk"])]:
        hist.put_item(Item={"store_id": "1546", "sk": f"{week}#0", "fetched_at": "t",
                            "deals": json.dumps([{"title": t, "savings": "$1.00"} for t in titles])})
        hist.put_item(Item={"store_id": "1546", "sk": week, "week": week, "fetched_at": "t", "num_chunks": 1})
    ddb.Table("test-sessions").put_item(Item={"token": "tok", "email": "ben@example.com",
                                             "expires_at": int(time.time()) + 3600})
    ddb.Table("test-users").put_item(Item={"email": "ben@example.com",
                                          "prefs": {"items": ["milk", "chips"]}})
    import deals
    deals._HISTORY_CACHE.clear()
    yield deals


def _event(qs, cookie="session=tok"):
    return {"queryStringParameters": qs, "cookies": [cookie], "headers": {}}


class TestHistoryEndpoint:
    def test_summary_shape_and_size(self, api_env):
        resp = api_env.get_deal_history(_event({"store_id": "1546"}))
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["version"] == 2 and body["num_weeks"] == 3
        assert body["badges"] == {"repeat": ["chips"], "rare": []}
        assert body["freq"] == {"milk": 1, "chips": 1}
        assert "snapshots" not in body
        assert len(resp["body"]) < 2000

    def test_title_points(self, api_env):
        body = json.loads(api_env.get_deal_history(_event({"store_id": "1546", "title": "Milk"}))["body"])
        assert [p["week"] for p in body["points"]] == [WEEKS[2], WEEKS[0]]

    def test_new_snapshot_after_scraper_invalidation(self, api_env, dynamodb_tables):
        api_env.get_deal_history(_event({"store_id": "1546"}))
        hist = dynamodb_tables.Table("test-deal-history")
        hist.put_item(Item={"store_id": "1546", "sk": "2026-10-12#0", "fetched_at": "t2",
                            "deals": json.dumps([{"title": "Milk", "savings": "$1"}])})
        hist.put_item(Item={"store_id": "1546", "sk": "2026-10-12", "week": "2026-10-12",
                            "fetched_at": "t2", "num_chunks": 1})
        # what scraper/main.py history_deals() now does after writing a snapshot
        hist.delete_item(Key={"store_id": "1546", "sk": "_summary"})
        body = json.loads(api_env.get_deal_history(_event({"store_id": "1546"}))["body"])
        assert body["num_weeks"] == 4 and body["newest_week"] == "2026-10-12"

    def test_summary_persisted_and_used_by_cold_instance(self, api_env, dynamodb_tables, monkeypatch):
        first = json.loads(api_env.get_deal_history(_event({"store_id": "1546"}))["body"])
        hist = dynamodb_tables.Table("test-deal-history")
        meta = hist.get_item(Key={"store_id": "1546", "sk": "_summary"})["Item"]
        assert int(meta["num_chunks"]) >= 1 and meta["newest_week"] == WEEKS[0]
        api_env._HISTORY_CACHE.clear()          # simulate a cold Lambda instance

        def boom(_):
            raise AssertionError("full history read should not happen")
        monkeypatch.setattr(api_env, "_load_history_snapshots", boom)
        second = json.loads(api_env.get_deal_history(_event({"store_id": "1546"}))["body"])
        assert second["badges"] == first["badges"] and second["freq"] == first["freq"]

    def test_summary_chunking(self, api_env, dynamodb_tables, monkeypatch):
        monkeypatch.setattr(api_env, "SUMMARY_CHUNK_BYTES", 50)
        api_env.get_deal_history(_event({"store_id": "1546"}))
        hist = dynamodb_tables.Table("test-deal-history")
        meta = hist.get_item(Key={"store_id": "1546", "sk": "_summary"})["Item"]
        assert int(meta["num_chunks"]) > 1
        api_env._HISTORY_CACHE.clear()
        body = json.loads(api_env.get_deal_history(_event({"store_id": "1546"}))["body"])
        assert body["badges"]["repeat"] == ["chips"]

    def test_stale_summary_rebuilt(self, api_env, dynamodb_tables):
        api_env.get_deal_history(_event({"store_id": "1546"}))
        hist = dynamodb_tables.Table("test-deal-history")
        hist.update_item(Key={"store_id": "1546", "sk": "_summary"},
                         UpdateExpression="SET built_at = :b",
                         ExpressionAttributeValues={":b": "2020-01-01T00:00:00+00:00"})
        api_env._HISTORY_CACHE.clear()
        body = json.loads(api_env.get_deal_history(_event({"store_id": "1546"}))["body"])
        assert body["num_weeks"] == 3
        meta = hist.get_item(Key={"store_id": "1546", "sk": "_summary"})["Item"]
        assert not meta["built_at"].startswith("2020")

    def test_requires_auth(self, api_env):
        assert api_env.get_deal_history(_event({"store_id": "1546"}, cookie="x=y"))["statusCode"] == 401


# ── Batch 0: per-environment cookies ──────────────────────────────────────────

class TestCookies:
    def test_make_cookie_defaults(self):
        import helpers
        c = helpers.make_cookie("session", "abc", 60)
        assert c == "session=abc; HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping; Path=/; Max-Age=60"

    def test_read_cookie_v2_and_v1(self):
        import helpers
        assert helpers.read_cookie({"cookies": ["a=1", "session_stg=xyz"]}, "session_stg") == "xyz"
        assert helpers.read_cookie({"headers": {"Cookie": "a=1; session=v1tok"}}, "session") == "v1tok"
        # a staging cookie must not be read as the production session
        assert helpers.read_cookie({"cookies": ["session_stg=xyz"]}, "session") == ""

    def test_host_only_when_domain_blank(self, monkeypatch):
        import helpers
        monkeypatch.setattr(helpers, "COOKIE_DOMAIN", "")
        assert "Domain=" not in helpers.make_cookie("s", "v", 1)
