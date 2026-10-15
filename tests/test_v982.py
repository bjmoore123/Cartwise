"""tests/test_v982.py — savings history (v9.8.2): per-week discount + deal type, no prices."""
import gzip
import json
import os
import shutil
import subprocess

import pytest

import history as H
from shared import email_render as ER

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def row(savings, st="WeeklyAd", save_line="", **kw):
    return {"title": "Chips", "savings": savings, "saving_type": st, "save_line": save_line, **kw}


class TestDealSaving:
    def test_stated_savings_per_item(self):
        assert H.deal_saving(row("$3.99", save_line="SAVE UP TO $1.50")) == {"s": 1.5, "k": "sale", "m": 1}
        assert H.deal_saving(row("$0.24", st="Tpr")) == {"s": 0.24, "k": "sale"}           # pre-v9.4 TPR row
        assert H.deal_saving(row("$0.50", st="Tpr", final_price=3.49, regular_price=3.99))["s"] == 0.5

    def test_bogo_is_halved_and_marked_up_to(self):
        assert H.deal_saving(row("Buy 1 Get 1 FREE", save_line="SAVE UP TO $5.49")) == {"s": 2.75, "k": "bogo", "m": 1}

    def test_kinds(self):
        assert H.deal_saving(row("2 for $6", save_line="SAVE UP TO $1.98"))["k"] == "multi"
        assert H.deal_saving(row("Save $2.00", st="DigitalCoupon")) == {"s": 2.0, "k": "coupon"}
        assert H.deal_saving(row("$3.00 off", st="ExtraSavings"))["k"] == "extra"

    def test_per_lb(self):
        assert H.deal_saving(row("$4.99 lb", save_line="SAVE UP TO $2.00 LB")) == {"s": 2.0, "k": "sale", "m": 1, "u": "lb"}

    def test_no_stated_amount_is_not_estimated(self):
        assert H.deal_saving(row("$3.99")) == {"s": None, "k": "sale"}
        assert H.deal_saving(row("Buy 1 Get 1 FREE")) == {"s": None, "k": "bogo"}
        assert H.deal_saving(row("40% off")) == {"s": None, "k": "sale", "p": 40}


class TestIndex:
    def _snaps(self, savings_lines):
        weeks = [f"2026-{m:02d}-01" for m in range(10, 10 - len(savings_lines), -1)]       # newest first
        return [{"week": w, "deals": [row("$3.99", save_line=sl)]} for w, sl in zip(weeks, savings_lines)]

    def test_points(self):
        idx = H.build_index([{"week": "2026-10-05", "deals": [row("Buy 1 Get 1 FREE", save_line="SAVE UP TO $5.49")]},
                             {"week": "2026-09-28", "deals": [row("$3.99")]}])
        assert H.title_points(idx, "Chips") == [
            {"week": "2026-09-28", "savings": "$3.99", "kind": "sale"},
            {"week": "2026-10-05", "savings": "Buy 1 Get 1 FREE", "kind": "bogo", "save": 2.75, "max": 1},
        ]

    def test_same_title_twice_keeps_biggest_saving(self):
        idx = H.build_index([{"week": "2026-10-05", "deals": [row("$3.99", save_line="SAVE $1"),
                                                              row("$3.49", save_line="SAVE $1.50"), row("$2.99")]}])
        assert H.title_points(idx, "Chips")[0]["save"] == 1.5

    def test_biggest_discount_badge(self):
        snaps = self._snaps(["SAVE $2.00"] + ["SAVE $1.00", "SAVE $1.50"] * 3 + ["SAVE $1.00"])
        b = H.compute_badges(H.build_index(snaps))
        assert b["best"] == ["chips"]

    def test_no_badge_when_saving_never_changes(self):
        assert H.compute_badges(H.build_index(self._snaps(["SAVE $1.00"] * 8)))["best"] == []

    def test_no_badge_below_the_best(self):
        snaps = self._snaps(["SAVE $1.00"] + ["SAVE $2.00", "SAVE $1.50"] * 3 + ["SAVE $1.00"])
        assert H.compute_badges(H.build_index(snaps))["best"] == []

    def test_summary_round_trip_and_old_formats(self):
        idx = H.build_index(self._snaps(["SAVE $2.00", "SAVE $1.00"]))
        back = H.deserialize_index(H.serialize_index(idx))
        assert H.title_points(back, "Chips") == H.title_points(idx, "Chips")
        assert H.SUMMARY_FMT == 5
        v94 = gzip.compress(json.dumps({"fmt": 3, "weeks": ["2026-10-05"],
                                        "titles": {"chips": ["Chips", [[0, "$2.99", 2.99]]]}}).encode())
        assert H.deserialize_index(v94)["titles"]["chips"][0] == ("Chips", "$2.99", None)    # a price is not a saving


class TestColors:
    def test_multibuy_is_teal_everywhere(self):
        assert ER.TAGS["multi"][1] == ER.TEAL and ER.TAGS["bogo"][1] == ER.AMBER
        css = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()
        assert ".deal-type-tag.multi{background:var(--multi-bg);color:var(--multi)}" in css
        assert "--multi:#0e6372" in css and ER.TEAL == "#0e6372"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_footer_and_tooltip_text():
    app = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    js = app[app.index("const DEAL_KIND_STYLE"):app.index("function buildDealChart")] \
        + app[app.index("function chartFooterHtml"):app.index("let _modalDeal = null;")]
    pts = [{"week": "2026-09-14", "savings": "$3.99", "kind": "sale", "save": 1.2},
           {"week": "2026-09-21", "savings": "BOGO", "kind": "bogo"},
           {"week": "2026-10-12", "savings": "Buy 1 Get 1 FREE", "kind": "bogo", "save": 2.85, "max": 1}]
    weeks = ["2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12"]
    js = ("const escH=s=>String(s);\n" + js + f"\nconst P={json.dumps(pts)},W={json.dumps(weeks)};"
          "console.log(JSON.stringify({foot:chartFooterHtml(P,W,'2026-10-12'),"
          "tips:P.map(saveText),wk:weekLabel('2026-10-12')}));")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "This week: save up to $2.85 ea" in out["foot"] and "best in 5 weeks" in out["foot"]
    assert "on sale 3 of 5 weeks" in out["foot"]
    assert all(k in out["foot"] for k in ("Sale", "BOGO", "Amount not listed", "Not on sale"))
    assert out["tips"] == ["Save $1.20 ea", "Amount not listed", "Save up to $2.85 ea"]
    assert out["wk"] == "Week of Oct 12, 2026"
