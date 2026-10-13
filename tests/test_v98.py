"""tests/test_v98.py — BOGO store rule: half price on one vs. buy two (v9.8)."""
import json
import os
import shutil
import subprocess
import time

import pytest

from shared.deal_parser import (classify_offer, apply_bogo_mode, bogo_mode_for_state,
                                state_from_address, effective_bogo_mode, compact_offer)
from shared import email_render as ER

B1G1 = compact_offer(classify_offer("WeeklyAd", "Buy 1 Get 1 FREE", "SAVE UP TO $8.99"))
B2G1 = compact_offer(classify_offer("WeeklyAd", "Buy 2 Get 1 FREE", "SAVE UP TO $4.99 ON 3"))
B1G1_LB = compact_offer(classify_offer("WeeklyAd", "Buy 1 Get 1 FREE", "SAVE UP TO $4.49 LB"))


class TestStateRules:
    @pytest.mark.parametrize("st,mode", [("FL", "two"), ("TN", "half"), ("NC", "half"), ("VA", "half"),
                                         ("KY", "half"), ("GA", None), ("AL", None), ("SC", None), ("", None)])
    def test_state_map(self, st, mode):
        assert bogo_mode_for_state(st) == mode

    def test_state_from_store_address(self):
        assert state_from_address("4920 Lakeland Hills Blvd, Lakeland, FL 33805") == "FL"
        assert state_from_address("123 Main St") == ""

    def test_effective_mode(self):
        assert effective_bogo_mode({"store_address": "1 A St, Tampa, FL 33602"}) == "two"
        assert effective_bogo_mode({"store_address": "1 A St, Nashville, TN 37203"}) == "half"
        assert effective_bogo_mode({"store_address": "1 A St, Atlanta, GA 30303"}) == "two"      # unknown → safe
        assert effective_bogo_mode({"store_address": "1 A St, Atlanta, GA 30303", "bogo_mode": "half"}) == "half"
        assert effective_bogo_mode({"store_address": "1 A St, Nashville, TN 37203", "bogo_mode": "two"}) == "two"
        assert effective_bogo_mode({}) == "two"


class TestPricing:
    def test_offer_records_quantities(self):
        assert (B1G1["buy"], B1G1["get"]) == (1, 1) and (B2G1["buy"], B2G1["get"]) == (2, 1)

    def test_half_price_store(self):
        o = apply_bogo_mode(B1G1, "half")
        assert o["context"] == "Half price: $4.50 ea, even if you buy 1"
        assert o["save_text"] == "Save up to $4.50 each"
        assert o["price"] == 4.5 and o["headline"] == "Buy 1 Get 1 Free"

    def test_buy_two_store(self):
        o = apply_bogo_mode(B1G1, "two")
        assert o["context"] == "≈ $4.50 ea when you buy 2"
        assert o["save_text"] == "Save up to $8.99"           # Publix's own total, unchanged

    def test_per_lb(self):
        assert apply_bogo_mode(B1G1_LB, "half")["save_text"] == "Save up to $2.25/lb"
        assert apply_bogo_mode(B1G1_LB, "two")["context"] == "≈ $2.25/lb when you buy 2"

    def test_buy_two_get_one_unchanged(self):
        assert apply_bogo_mode(B2G1, "half") == B2G1

    def test_non_bogo_unchanged(self):
        o = compact_offer(classify_offer("Tpr", "$0.24", "", 2.15, 2.39))
        assert apply_bogo_mode(o, "half") == o

    def test_no_price_known(self):
        o = compact_offer(classify_offer("WeeklyAd", "Buy 1 Get 1 FREE"))
        assert apply_bogo_mode(o, "half")["context"] == "Half price each, even if you buy 1"


class TestEmail:
    def _m(self):
        return {"my_item": "cereal", "deal_name": "Cheerios", "saving_type": "WeeklyAd", "is_bogo": True,
                "offer": B1G1}

    def test_email_follows_mode(self):
        half = ER.build_html([self._m()], "1", "S", bogo_mode="half")
        two = ER.build_html([self._m()], "1", "S", bogo_mode="two")
        assert "even if you buy 1" in half and "Save up to $4.50 each" in half
        assert "when you buy 2" in two and "Save up to $8.99" in two
        assert "even if you buy 1" in ER.build_text([self._m()], "S", bogo_mode="half")


class TestPrefs:
    def test_save_and_validate(self, dynamodb_tables):
        import prefs
        dynamodb_tables.Table("test-sessions").put_item(Item={"token": "tok", "email": "ben@example.com",
                                                             "expires_at": int(time.time()) + 3600})
        dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com", "prefs": {}})
        ev = {"queryStringParameters": {}, "cookies": ["session=tok"], "headers": {}}
        r = prefs.save_prefs(ev, {"prefs": {"bogo_mode": "half", "items": []}})
        assert json.loads(r["body"])["prefs"]["bogo_mode"] == "half"
        r = prefs.save_prefs(ev, {"prefs": {"bogo_mode": "free-for-all", "items": []}})
        assert json.loads(r["body"])["prefs"]["bogo_mode"] == "auto"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_bogo_rules_match_python():
    """frontend applyBogoMode / bogoModeForState must give the same text as Python."""
    root = os.path.join(os.path.dirname(__file__), "..")
    app = open(os.path.join(root, "frontend", "app.js"), encoding="utf-8").read()
    start = app.index("const BOGO_HALF_STATES")
    end = app.index("function bogoCardHtml")
    cases = [(o, m) for o in (B1G1, B2G1, B1G1_LB) for m in ("half", "two")]
    js = app[start:end] + "\nconst cases=" + json.dumps(cases) + ";\nconsole.log(JSON.stringify(" \
         "{modes:['FL','TN','GA','KY','AL'].map(bogoModeForState), out:cases.map(([o,m])=>applyBogoMode(o,m))}));"
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["modes"] == ["two", "half", None, "half", None]
    assert got["out"] == [apply_bogo_mode(o, m) for o, m in cases]
