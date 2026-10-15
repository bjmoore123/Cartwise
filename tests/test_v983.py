"""tests/test_v983.py — Admin user table: last login (v9.8.3)."""
import json

import pytest


@pytest.fixture
def as_admin(dynamodb_tables, monkeypatch):
    import admin
    monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
    return admin


def _users(admin):
    return {u["email"]: u for u in json.loads(admin.admin_list_users({"queryStringParameters": {}})["body"])["users"]}


def test_login_records_last_login(dynamodb_tables, monkeypatch):
    import admin, auth
    monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
    dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com", "pin_hash": "x", "email_verified": True})
    admin.admin_reset_pin({}, {"new_pin": "4821"}, "ben@example.com")
    ev = {"headers": {}, "requestContext": {"http": {"sourceIp": "1.2.3.4"}}, "cookies": []}
    assert auth.login(ev, {"email": "ben@example.com", "pin": "4821"})["statusCode"] == 200
    u = dynamodb_tables.Table("test-users").get_item(Key={"email": "ben@example.com"})["Item"]
    assert u["last_login"].startswith("20")
    assert _users(admin)["ben@example.com"]["last_login"] == u["last_login"]


def test_backfill_from_auth_log_once(dynamodb_tables, as_admin):
    users, logs = dynamodb_tables.Table("test-users"), dynamodb_tables.Table("test-auth-logs")
    users.put_item(Item={"email": "a@example.com"})
    users.put_item(Item={"email": "b@example.com"})
    users.put_item(Item={"email": "c@example.com", "last_login": "2026-10-14T12:00:00+00:00"})
    for i, (em, ts, ok) in enumerate([("a@example.com", "2026-09-01T10:00:00+00:00", True),
                                      ("a@example.com", "2026-10-01T10:00:00+00:00", True),
                                      ("a@example.com", "2026-10-09T10:00:00+00:00", False),   # failed: ignored
                                      ("c@example.com", "2026-01-01T00:00:00+00:00", True)]):
        logs.put_item(Item={"log_id": f"l{i}", "email": em, "ts": ts, "success": ok})
    got = _users(as_admin)
    assert got["a@example.com"]["last_login"] == "2026-10-01T10:00:00+00:00"
    assert got["b@example.com"]["last_login"] == ""                                       # never signed in
    assert got["c@example.com"]["last_login"] == "2026-10-14T12:00:00+00:00"               # recorded value wins
    assert users.get_item(Key={"email": "a@example.com"})["Item"]["last_login"] == "2026-10-01T10:00:00+00:00"


def test_table_has_sortable_headers():
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    html = open(os.path.join(root, "frontend", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "frontend", "app.js"), encoding="utf-8").read()
    for key in ("email", "notify", "store", "items", "joined", "last_login"):
        assert f'data-action="admin-sort-users" data-sort="{key}"' in html
        assert f"{key}:" in js[js.index("const ADMIN_USER_SORT"):js.index("const ADMIN_SORT_FIRST_DIR")]
    assert "if (action === 'admin-sort-users')" in js


def test_user_list_still_requires_admin(dynamodb_tables, monkeypatch):
    import admin
    monkeypatch.setattr(admin, "get_admin_auth", lambda e: False)
    assert admin.admin_list_users({"queryStringParameters": {}})["statusCode"] in (401, 403)
