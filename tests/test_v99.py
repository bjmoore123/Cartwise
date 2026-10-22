"""tests/test_v99.py — in-app feedback; verify-email on Notifications (v9.9)."""
import json
import os
import sys
import time
import types

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _ev(headers=None):
    return {"queryStringParameters": {}, "cookies": ["session=tok"], "headers": headers or {"User-Agent": "Mozilla/5.0 Test"}}


@pytest.fixture
def user(dynamodb_tables, monkeypatch):
    dynamodb_tables.Table("test-sessions").put_item(Item={"token": "tok", "email": "ben@example.com",
                                                         "expires_at": int(time.time()) + 3600})
    dynamodb_tables.Table("test-users").put_item(Item={"email": "ben@example.com", "email_verified": True,
                                                      "prefs": {"store_id": "1546", "store_name": "Lakeland Hills"}})
    sent = []
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=sent.append)))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("FEEDBACK_TO", "admin@example.com")
    import feedback
    return types.SimpleNamespace(ddb=dynamodb_tables, sent=sent, fb=feedback)


def _send(u, **body):
    r = u.fb.submit_feedback(_ev(), {"kind": "idea", "message": "Add a dark mode please", **body})
    return r["statusCode"], json.loads(r["body"])


def _rows(u):
    return u.ddb.Table("test-feedback").scan()["Items"]


class TestSubmit:
    def test_saved_and_emailed(self, user):
        code, body = _send(user, page="deals", app_version="9.9")
        assert code == 200 and "Thanks" in body["message"]
        (row,) = _rows(user)
        assert row["email"] == "ben@example.com" and row["kind"] == "idea" and row["status"] == "new"
        assert row["page"] == "deals" and row["store_id"] == "1546" and row["user_agent"] == "Mozilla/5.0 Test"
        assert int(row["expires_at"]) > time.time() + 300 * 86400
        (mail,) = user.sent
        assert mail["to"] == ["admin@example.com"] and mail["reply_to"] == ["ben@example.com"]
        assert mail["subject"].startswith("[Cartwise feedback] Idea from ben@example.com")

    def test_reply_to_is_the_session_never_the_body(self, user):
        _send(user, email="attacker@evil.test", reply_to="attacker@evil.test")
        assert user.sent[0]["reply_to"] == ["ben@example.com"] and _rows(user)[0]["email"] == "ben@example.com"

    def test_message_is_escaped_and_subject_single_line(self, user):
        _send(user, message='<script>alert(1)</script>\r\nBcc: x@evil.test\x07')
        mail = user.sent[0]
        assert "<script>" not in mail["html"] and "&lt;script&gt;" in mail["html"]
        assert "\n" not in mail["subject"] and "\r" not in mail["subject"]
        assert "\x07" not in _rows(user)[0]["message"]

    @pytest.mark.parametrize("body,code", [({"kind": "rant"}, 400), ({"message": "   "}, 400),
                                           ({"message": "x" * 2001}, 400)])
    def test_validation(self, user, body, code):
        assert _send(user, **body)[0] == code and not _rows(user)

    def test_page_must_be_known(self, user):
        _send(user, page="<img src=x>")
        assert "page" not in _rows(user)[0]

    def test_unverified_blocked(self, user):
        user.ddb.Table("test-users").update_item(Key={"email": "ben@example.com"},
                                                 UpdateExpression="SET email_verified = :f", ExpressionAttributeValues={":f": False})
        assert _send(user)[0] == 403 and not _rows(user)

    def test_signed_out_blocked(self, user):
        r = user.fb.submit_feedback({"queryStringParameters": {}, "cookies": [], "headers": {}}, {"kind": "idea", "message": "hi"})
        assert r["statusCode"] == 401

    def test_rate_limited_after_five_an_hour(self, user):
        codes = [_send(user, message=f"m{i}")[0] for i in range(6)]
        assert codes == [200] * 5 + [429] and len(_rows(user)) == 5

    def test_email_failure_still_saves(self, user, monkeypatch):
        def boom(m): raise RuntimeError("resend down")
        monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=boom)))
        assert _send(user)[0] == 200 and len(_rows(user)) == 1

    def test_no_address_configured_still_saves(self, user, monkeypatch):
        monkeypatch.setenv("FEEDBACK_TO", "")
        assert _send(user)[0] == 200 and len(_rows(user)) == 1 and not user.sent


class TestAdmin:
    def test_list_update_and_guard(self, user, monkeypatch):
        import admin
        _send(user, message="first"); _send(user, kind="bug", message="second")
        monkeypatch.setattr(admin, "get_admin_auth", lambda e: False)
        assert admin.admin_feedback_list({})["statusCode"] in (401, 403)
        monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
        body = json.loads(admin.admin_feedback_list({})["body"])
        assert [f["message"] for f in body["feedback"]] == ["second", "first"] and body["new"] == 2
        fid = body["feedback"][0]["feedback_id"]
        assert admin.admin_feedback_update({}, {"status": "done"}, fid)["statusCode"] == 200
        assert json.loads(admin.admin_feedback_list({})["body"])["new"] == 1
        assert admin.admin_feedback_update({}, {"status": "deleted"}, fid)["statusCode"] == 400
        assert admin.admin_feedback_update({}, {"status": "done"}, "nope")["statusCode"] == 404


class TestVerifyState:
    def test_prefs_report_verified(self, user):
        import prefs
        assert json.loads(prefs.get_prefs(_ev())["body"])["email_verified"] is True
        user.ddb.Table("test-users").update_item(Key={"email": "ben@example.com"},
                                                 UpdateExpression="SET email_verified = :f", ExpressionAttributeValues={":f": False})
        assert json.loads(prefs.get_prefs(_ev())["body"])["email_verified"] is False


class TestWiring:
    def test_routes_deploy_teardown(self):
        h = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert '"/user/feedback"' in h and '"/admin/feedback"' in h and '"/admin/feedback/"' in h
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert 'create_table "${APP_NAME}-feedback"' in d and 'enable_ttl "feedback"' in d
        assert 'table/${APP_NAME}-feedback' in d and d.count('"FEEDBACK_TO" "${FEEDBACK_TO_VAL}"') == 3
        assert "/publix/feedback-to" in d and "moore.ben" not in d          # the address lives only in SSM
        assert '"${APP_NAME}-feedback"' in open(os.path.join(_ROOT, "teardown.sh"), encoding="utf-8").read()

    def test_frontend(self):
        js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
        html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()
        assert 'id="nav-feedback"' in html and 'id="mnav-feedback"' in html and 'id="verify-card"' in html
        assert 'maxlength="2000"' in html and 'id="apage-feedback"' in html
        assert "emailVerified: data.email_verified !== false" in js          # reload keeps the banner
        assert "${escH(f.message||'')}" in js                                # admin view escapes messages
        assert 'href="${escH(reply)}" target="_blank" rel="noopener noreferrer"' in js   # Reply opens a new tab
        assert "moore.ben" not in js and "moore.ben" not in html


class TestPackaging:
    """v9.9.1: feedback.py was missing from deploy.sh's fixed file list, so the API
    crashed on startup (every request 500, including CORS preflight)."""

    def test_every_lambda_module_is_packaged(self):
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert 'for f in "${SCRIPT_DIR}"/lambda/*.py; do' in d

    def test_every_local_import_exists(self):
        import ast
        lam = os.path.join(_ROOT, "lambda")
        local = {f[:-3] for f in os.listdir(lam) if f.endswith(".py")}
        for f in sorted(local):
            tree = ast.parse(open(os.path.join(lam, f + ".py"), encoding="utf-8").read())
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                       [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
                for m in mods:
                    top = m.split(".")[0]
                    if top in local or top in ("feedback", "admin", "auth", "prefs", "deals", "history", "inbound"):
                        assert top in local, f"lambda/{f}.py imports {m}, which is not in lambda/"

    def test_deploy_smoke_tests_the_api(self):
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert "API smoke test failed" in d and '"method":"OPTIONS"' in d and "exit 1" in d
