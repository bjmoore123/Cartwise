"""tests/test_matcher_parity.py — v9.4 (#10): the Python matcher (emails) and the
JS port (web page) must give identical scores. Fixture: 3,222 real item/deal pairs."""
import json
import os
import shutil
import subprocess

import pytest

from shared.matcher import _match_score

_HERE = os.path.dirname(__file__)
_CASES = json.load(open(os.path.join(_HERE, "fixtures", "matcher_parity.json"), encoding="utf-8"))


def test_python_scores_match_fixture():
    diffs = [(c["item"], c["deal"]["title"], c["py"], _match_score(c["item"], c["deal"]))
             for c in _CASES if _match_score(c["item"], c["deal"]) != c["py"]]
    assert not diffs, diffs[:5]


def test_punctuation_is_deleted_not_spaced():
    from shared.matcher import normalize
    assert normalize("Ben & Jerry's") == "ben jerrys"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_matches_python():
    r = subprocess.run(["node", os.path.join(_HERE, "matcher_parity.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
