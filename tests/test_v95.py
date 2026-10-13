"""tests/test_v95.py — v9.5 accounts and security fixes."""
import json
import sys
import time
import types

import pytest


def _ev(token="tok"):
    return {"queryStringParameters": {}, "cookies": [f"session={token}"], "headers": {}}


def _session(ddb, token, email):
    ddb.Table("test-sessions").put_item(Item={"token": token, "email": email,
                                             "expires_at": int(time.time()) + 3600,
                                             "session_created_at": int(time.time()) - 10})


@pytest.fixture
def users(dynamodb_tables):
    t = dynamodb_tables.Table("test-users")
    t.put_item(Item={"email": "ben@example.com", "pin_hash": "x", "prefs": {"items": []}})
    return dynamodb_tables


@pytest.fixture
def as_admin(monkeypatch):
    import admin
    monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
    return admin


class TestSessions:
    def test_session_of_deleted_account_is_dead(self, users):
        import helpers
        _session(users, "tok", "ben@example.com")
        assert helpers.get_session(_ev()) is not None
        users.Table("test-users").delete_item(Key={"email": "ben@example.com"})
        assert helpers.get_session(_ev()) is None

    def test_delete_account_signs_out_every_device(self, users):
        import prefs
        for t in ("tok", "phone", "laptop"):
            _session(users, t, "ben@example.com")
        _session(users, "other", "amy@example.com")
        assert prefs.delete_account(_ev())["statusCode"] == 200
        left = {i["token"] for i in users.Table("test-sessions").scan()["Items"]}
        assert left == {"other"}
        meta = users.Table("test-users").get_item(Key={"email": "__meta__"})["Item"]
        assert int(meta["deleted_count"]) == 1

    def test_admin_email_change_kills_old_sessions(self, users, as_admin):
        _session(users, "tok", "ben@example.com")
        r = as_admin.admin_reset_email({}, {"new_email": "ben2@example.com"}, "ben@example.com")
        assert r["statusCode"] == 200
        import helpers
        assert helpers.get_session(_ev()) is None
        assert not users.Table("test-sessions").scan()["Items"]

    def test_admin_delete_revokes_and_counts(self, users, as_admin):
        _session(users, "tok", "ben@example.com")
        r = as_admin.admin_delete_user({}, "ben@example.com")
        assert "1 session signed out" in json.loads(r["body"])["message"]
        assert not users.Table("test-sessions").scan()["Items"]


class TestUnsubscribeFailsClosed:
    def test_no_secret_rejects_every_token(self, users, monkeypatch):
        import helpers, prefs
        monkeypatch.setattr(helpers, "UNSUB_SECRET", "")
        monkeypatch.setattr(prefs, "UNSUB_SECRET", "")
        assert helpers._verify_unsub_token("ben@example.com", "anything") is False
        r = prefs.unsubscribe({"queryStringParameters": {"email": "ben@example.com", "token": "abc"},
                               "requestContext": {"http": {"method": "POST"}}})
        assert "Configuration Error" in r["body"]
        assert users.Table("test-users").get_item(Key={"email": "ben@example.com"})["Item"]["prefs"] == {"items": []}

    def test_with_secret_round_trip(self, monkeypatch):
        import helpers
        monkeypatch.setattr(helpers, "UNSUB_SECRET", "s3cret")
        assert helpers._verify_unsub_token("Ben@Example.com", helpers._unsub_token("ben@example.com"))

    def test_no_placeholder_default_in_code(self):
        import inspect, helpers
        src = inspect.getsource(helpers)
        assert 'os.environ.get("UNSUB_SECRET", "changeme' not in src


class TestVerificationLink:
    def test_plus_address_is_encoded(self, monkeypatch):
        import auth
        sent = {}
        fake = types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=lambda m: sent.update(m)))
        monkeypatch.setitem(sys.modules, "resend", fake)
        monkeypatch.setenv("RESEND_API_KEY", "re_test")
        auth._send_verification_email("ben+test@example.com", "tok<>")
        assert "email=ben%2Btest%40example.com" in sent["html"]
        assert "token=tok%3C%3E" in sent["html"]


class TestAdminLogsNewest:
    def test_scrape_logs_latest_first_beyond_one_page(self, dynamodb_tables, as_admin):
        t = dynamodb_tables.Table("test-scrape-logs")
        for i in range(120):
            t.put_item(Item={"job_id": f"job{i:03d}", "started_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}"})
        logs = json.loads(as_admin.get_scrape_logs({})["body"])["logs"]
        assert logs[0]["job_id"] == "job119" and len(logs) == 15

    def test_inbound_logs_read_all(self, dynamodb_tables, as_admin):
        t = dynamodb_tables.Table("test-app-logs")
        for i in range(30):
            t.put_item(Item={"log_id": f"l{i}", "ts": f"2026-01-01T00:00:{i:02d}", "action": "inbound-import"})
        body = json.loads(as_admin.admin_inbound_logs({"queryStringParameters": {}})["body"])
        assert len(body["logs"]) == 30 and body["logs"][0]["log_id"] == "l29"


class TestRegisterExisting:
    def test_existing_email_gets_the_generic_answer(self, users):
        import auth
        r = auth.register({"email": "ben@example.com", "pin": "4821"})
        assert r["statusCode"] == 201 and "If this email is not already registered" in r["body"]


class TestAdminSecretRotation:
    @pytest.fixture
    def admin_tbl(self, dynamodb_tables, monkeypatch):
        t = dynamodb_tables.create_table(
            TableName="test-admin-sessions", KeySchema=[{"AttributeName": "token", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "token", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        import helpers, admin
        monkeypatch.setattr(helpers, "admin_sessions_tbl", t)
        monkeypatch.setattr(admin, "admin_sessions_tbl", t)
        return t

    def _login(self, admin):
        r = admin.admin_login({"headers": {}}, {"secret": admin.ADMIN_SECRET})
        assert r["statusCode"] == 200
        return r["cookies"][0].split(";")[0].split("=", 1)[1]

    def test_rotating_the_secret_ends_admin_sessions(self, admin_tbl, monkeypatch):
        import helpers, admin
        tok = self._login(admin)
        ev = {"headers": {}, "cookies": [f"{helpers.ADMIN_COOKIE_NAME}={tok}"]}
        assert helpers.get_admin_auth(ev) is True
        monkeypatch.setattr(helpers, "ADMIN_SECRET", "a-brand-new-secret")
        assert helpers.get_admin_auth(ev) is False

    def test_pre_v95_session_without_fingerprint_is_rejected(self, admin_tbl):
        import helpers
        admin_tbl.put_item(Item={"token": "old", "expires_at": int(time.time()) + 3600})
        assert helpers.get_admin_auth({"headers": {}, "cookies": [f"{helpers.ADMIN_COOKIE_NAME}=old"]}) is False
