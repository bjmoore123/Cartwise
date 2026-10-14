"""tests/test_v981.py — scraper fan-out must invoke its own function (v9.8.1).

Staging's orchestrator invoked "cartwise-scraper" (production) because the
scraper Lambda never had SCRAPER_FUNCTION set; IAM refused it.
"""
import importlib.util
import os
import re
import sys
import types

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture
def scraper_main(dynamodb_tables, monkeypatch):
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=None))
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    sys.modules.pop("scraper", None)
    spec = importlib.util.spec_from_file_location("cw_scraper_main981", os.path.join(_ROOT, "scraper", "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("scraper", None)


def _run(mod, monkeypatch):
    calls = []

    class FakeLambda:
        def invoke(self, **kw):
            calls.append(kw["FunctionName"])

    monkeypatch.setattr(mod, "get_all_users", lambda: [{"email": "a@example.com", "prefs": {"store_id": "1825"}}])
    monkeypatch.setattr(mod, "write_scrape_log", lambda job: None)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: FakeLambda())
    mod._orchestrator(send_emails=False)
    return calls


def test_staging_invokes_itself_without_env(scraper_main, monkeypatch):
    monkeypatch.delenv("SCRAPER_FUNCTION", raising=False)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "cartwise-staging-scraper")
    assert _run(scraper_main, monkeypatch) == ["cartwise-staging-scraper"]


def test_explicit_env_wins(scraper_main, monkeypatch):
    monkeypatch.setenv("SCRAPER_FUNCTION", "cartwise-scraper")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "something-else")
    assert _run(scraper_main, monkeypatch) == ["cartwise-scraper"]


def test_deploy_sets_scraper_function_on_scraper_lambda():
    sh = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
    blocks = re.findall(r'apply_env "\$\{LAMBDA_SCRAPER\}" \\\n((?:\s+".*\\\n)*\s+".*)', sh)
    assert len(blocks) == 3
    assert all('"SCRAPER_FUNCTION" "${LAMBDA_SCRAPER}"' in b for b in blocks)


def test_frontend_error_filter_present():
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    assert "webkit-masked-url" in js and "_reportableError" in js
