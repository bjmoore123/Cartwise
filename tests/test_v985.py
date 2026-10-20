"""tests/test_v985.py — autocomplete relevance and list cleanup (v9.8.5)."""
import json
import os
import shutil
import subprocess

import pytest

import history as H

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _terms(titles, brands=()):
    return H.autocomplete_terms({"titles": {"x": {i: (t, "", None) for i, t in enumerate(titles)}},
                                 "brands": list(brands)})


class TestCleanup:
    def test_one_spelling_per_product(self):
        assert _terms(["Boar's Head® Deluxe Ham", "Boar's Head Deluxe Ham", "Olé Corn Tostadas",
                       "Ole Corn Tostadas", "CHEERIOS", "Cheerios"]) == \
            ["Boar's Head Deluxe Ham", "Cheerios", "Ole Corn Tostadas"]

    def test_quotes_stripped(self):
        assert _terms(['"Dirty" Deli Style Potato Chips'], ['"Dirty"', '"""I and love and you"""']) == \
            ["Dirty", "Dirty Deli Style Potato Chips", "I and love and you"]

    def test_promos_and_fine_print_dropped(self):
        junk = ["$10 off When You Spend $30 on Align, Crest", "Save $2.00 on any ONE (1) 12ct Fancy Feast",
                "(excludes trial/travel size)", ".35–3.5-oz", "Lactaid\r\nor Tylenol", "x" * 101]
        assert _terms(junk + ["Publix Large Eggs"]) == ["Publix Large Eggs"]


def test_promo_filter_in_sync():
    """Same not-a-product rule in the browser and on the server."""
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    py = open(os.path.join(_ROOT, "lambda", "history.py"), encoding="utf-8").read()
    js_re = js[js.index("const AC_NOT_PRODUCT = /") + len("const AC_NOT_PRODUCT = /"):js.index("/i;", js.index("const AC_NOT_PRODUCT"))]
    py_re = py[py.index('_AC_PROMO = re.compile(r"') + len('_AC_PROMO = re.compile(r"'):py.index('", re.I)', py.index("_AC_PROMO = re.compile"))]
    assert js_re == py_re
    assert "raw.length > 100" in js and "_AC_MAX_LEN = 100" in py
    assert "cw_corpus_v4_" in js and "/deals/corpus?v=2" in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_ranking():
    app = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    code = app[app.index("let _acCache"):app.index("function acInput()")]
    corpus = ["4C Grated Cheese", "4C Grated Cheese, Parmesan", "Publix American Cheese", "Kraft American Cheese Singles",
              "Milk Duds", "Milk-Bone Dog Biscuits", "Publix Whole Milk", "Lactaid Milk", "Boar's Head® Deluxe Ham",
              "Boar's Head Deluxe Ham", "Publix Large Eggs", "Eggland's Best Eggs"]
    js = (f"let _allDeals=[{{title:\"Boar's Head American Cheese\"}},{{title:\"Save $1.50 on any ONE (1) Sargento Natural American Sliced Cheese Product\"}}],_corpusTitles={json.dumps(corpus)},_corpusStore='1';"
          "const itemNames=()=>['eggland\\'s best eggs'];" + code +
          "const T=acGetTitles(),r=q=>acRank(q,T);"
          "console.log(JSON.stringify({ac:r('american chees').top.map(x=>x.t),milk:r('milk').top.map(x=>x.t),"
          "ham:r('boars head ham').top.map(x=>x.t),eggs:r('eggs').top.map(x=>x.t),sale:r('american chees').top[0].sale,"
          "n:T.length}));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["ac"][0] == "Boar's Head American Cheese" and got["sale"] is True
    assert set(got["ac"]) == {"Boar's Head American Cheese", "Publix American Cheese", "Kraft American Cheese Singles"}
    assert got["milk"][:2] == ["Lactaid Milk", "Publix Whole Milk"]          # product noun beats "Milk Duds"
    assert len(got["ham"]) == 1                                              # ® spelling merged
    assert got["eggs"] == ["Publix Large Eggs"]                              # already on the list: hidden
    assert got["n"] == 11
