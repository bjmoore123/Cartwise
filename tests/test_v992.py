"""tests/test_v992.py — admin session: check endpoint, lock on expiry, ended on sign-out (v9.9.2)."""
import json
import os
import time

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _ev(tok="adm"):
    import helpers
    return {"queryStringParameters": {}, "cookies": [f"{helpers.ADMIN_COOKIE_NAME}={tok}"], "headers": {}}


@pytest.fixture
def adm(dynamodb_tables, monkeypatch):
    import admin, helpers
    t = dynamodb_tables.create_table(
        TableName="test-admin-sessions", KeySchema=[{"AttributeName": "token", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "token", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
    monkeypatch.setattr(helpers, "admin_sessions_tbl", t)
    monkeypatch.setattr(admin, "admin_sessions_tbl", t)
    return t


def _put(tbl, expires_in, fp=None):
    import helpers
    tbl.put_item(Item={
        "token": "adm", "expires_at": int(time.time()) + expires_in,
        "secret_fp": helpers.admin_secret_fp() if fp is None else fp})


class TestSessionEndpoint:
    def test_valid_session_reports_time_left(self, adm):
        import admin
        _put(adm, 3600)
        r = admin.admin_session_info(_ev())
        body = json.loads(r["body"])
        assert r["statusCode"] == 200 and 3590 <= body["expires_in"] <= 3600

    def test_expired_missing_or_rotated_is_401(self, adm):
        import admin
        assert admin.admin_session_info(_ev())["statusCode"] == 401              # no such session
        _put(adm, -5)
        assert admin.admin_session_info(_ev())["statusCode"] == 401              # expired
        _put(adm, 3600, fp="old-secret")
        assert admin.admin_session_info(_ev())["statusCode"] == 401              # secret rotated
        assert admin.admin_session_info({"cookies": [], "headers": {}})["statusCode"] == 401

    def test_routed(self):
        h = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert 'path == "/admin/session"      and method == "GET"' in h


class TestFrontend:
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()

    def _fn(self, name):
        a = self.js.index(name); return self.js[a:self.js.index("\n}\n", a)]

    def test_admin_401_locks_dashboard(self):
        assert "url.startsWith(API + '/admin/')" in self.js and "adminLock('Your admin session has expired" in self.js

    def test_dashboard_only_shown_after_server_says_yes(self):
        init = self._fn("async function initAdmin(){")
        assert "adminCheckSession()" in init and "/admin/scrape-logs" not in init
        assert init.index("valid===true") < init.index("getElementById('admin-dash').style.display='block'")
        assert "adminLock(" in init

    def test_lock_clears_data_and_shows_prompt(self):
        lock = self._fn("function adminLock(msg){")
        for needle in ("_adminLoggedIn=false", "_adminDataLoaded=false", "_allAdminUsers=[]", "_adminFeedback=[]",
                       "_adminBlankHtml", "getElementById('admin-dash').style.display='none'",
                       "getElementById('admin-login-sec').style.display=''"):
            assert needle in lock, needle

    def test_user_sign_out_ends_admin_session(self):
        out = self._fn("async function doLogout(opts){")
        assert "/admin/logout" in out and "adminLock('')" in out

    def test_rechecked_on_focus_and_at_expiry(self):
        assert "if (_adminLoggedIn) adminCheckSession();" in self._fn("function checkSession(force) {")
        assert "_adminExpiryTimer=setTimeout(adminCheckSession" in self.js
