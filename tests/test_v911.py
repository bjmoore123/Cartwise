"""tests/test_v911.py — Forgot PIN from the sign-in page; verification reminders (v9.11)."""
import hashlib
import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
EMAIL = "ben@example.com"


def _ev(ip="1.2.3.4"):
    return {"headers": {}, "cookies": [], "queryStringParameters": {},
            "requestContext": {"http": {"sourceIp": ip, "method": "POST", "path": "/auth/forgot-pin"}}}


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=sent.append)))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    return sent


@pytest.fixture
def auth(dynamodb_tables, mail, monkeypatch):
    import auth as A, helpers
    monkeypatch.setattr(A, "PIN_RESET_MIN_SECONDS", 0)
    dynamodb_tables.Table("test-users").put_item(Item={
        "email": EMAIL, "pin_hash": helpers.hash_pin("4821"), "email_verified": True, "prefs": {},
        "failed_attempts": 4, "lockout_until": "2099-01-01T00:00:00+00:00"})
    return A


def _users(ddb):
    return ddb.Table("test-users")


def _link(mail):
    html = mail[-1]["html"]
    at = html.rindex('href="', 0, html.index("/#reset?")) + 6
    url = html[at:html.index('"', at)].replace("&amp;", "&")
    assert "/#reset?" in url
    q = parse_qs(url.split("#reset?", 1)[1])
    return q["token"][0], q["email"][0]


class TestForgotPin:
    def test_same_answer_whether_or_not_the_account_exists(self, auth, mail, dynamodb_tables):
        known = auth.forgot_pin(_ev(), {"email": EMAIL})
        unknown = auth.forgot_pin(_ev(), {"email": "nobody@example.com"})
        assert known["statusCode"] == unknown["statusCode"] == 200
        assert json.loads(known["body"]) == json.loads(unknown["body"]) == {"message": auth.PIN_RESET_REQUEST_MSG}
        assert [m["to"] for m in mail] == [[EMAIL]]                       # only the real account gets mail
        assert not _users(dynamodb_tables).get_item(Key={"email": "nobody@example.com"}).get("Item")

    def test_rate_limited_requests_look_the_same(self, auth, mail):
        bodies = [json.loads(auth.forgot_pin(_ev(), {"email": EMAIL})["body"]) for _ in range(5)]
        assert all(b == {"message": auth.PIN_RESET_REQUEST_MSG} for b in bodies)
        assert len(mail) == 3                                                # 3 an hour per email

    def test_ip_limit(self, auth, mail):
        for i in range(12):
            auth.forgot_pin(_ev("9.9.9.9"), {"email": f"x{i}@example.com"})
        assert auth.forgot_pin(_ev("9.9.9.9"), {"email": EMAIL})["statusCode"] == 200 and not mail

    def test_minimum_response_time(self, auth, monkeypatch):
        waits = []
        monkeypatch.setattr(auth, "PIN_RESET_MIN_SECONDS", 1.5)
        monkeypatch.setattr(auth.time, "sleep", waits.append)
        auth.forgot_pin(_ev(), {"email": "nobody@example.com"})
        assert waits and 1.0 < waits[0] <= 1.5

    def test_bad_email_is_about_the_input_only(self, auth, mail):
        r = auth.forgot_pin(_ev(), {"email": "not-an-email"})
        assert r["statusCode"] == 400 and "valid email" in json.loads(r["body"])["error"] and not mail

    def test_only_a_hash_is_stored_and_the_link_uses_the_fragment(self, auth, mail, dynamodb_tables):
        auth.forgot_pin(_ev(), {"email": EMAIL})
        token, email = _link(mail)
        row = _users(dynamodb_tables).get_item(Key={"email": EMAIL})["Item"]
        assert email == EMAIL and token not in json.dumps(row, default=str)
        assert row["pin_reset_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert int(row["pin_reset_exp"]) - time.time() == pytest.approx(30 * 60, abs=5)


class TestResetPin:
    def _token(self, auth, mail):
        auth.forgot_pin(_ev(), {"email": EMAIL}); return _link(mail)[0]

    def test_reset_works_once_and_cleans_up(self, auth, mail, dynamodb_tables, monkeypatch):
        import helpers
        revoked = []
        monkeypatch.setattr(auth, "revoke_sessions", revoked.append)
        _users(dynamodb_tables).update_item(Key={"email": EMAIL}, UpdateExpression="SET email_verified = :f",
                                            ExpressionAttributeValues={":f": False})
        token = self._token(auth, mail)
        r = auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "7395"})
        assert r["statusCode"] == 200
        row = _users(dynamodb_tables).get_item(Key={"email": EMAIL})["Item"]
        assert helpers.verify_pin("7395", row["pin_hash"]) and row["email_verified"] is True
        for gone in ("pin_reset_hash", "pin_reset_exp", "failed_attempts", "lockout_until"):
            assert gone not in row
        assert revoked == [EMAIL] and mail[-1]["subject"] == "Your Cartwise PIN was changed"
        again = auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "1593"})
        assert again["statusCode"] == 400 and json.loads(again["body"])["error"] == auth.PIN_RESET_BAD_LINK_MSG

    def test_bad_links_all_get_the_same_message(self, auth, mail, dynamodb_tables):
        token = self._token(auth, mail)
        cases = [{"email": EMAIL, "token": "wrong"}, {"email": "nobody@example.com", "token": token},
                 {"email": EMAIL, "token": ""}]
        msgs = {json.loads(auth.reset_pin(_ev(), {**c, "new_pin": "7395"})["body"])["error"] for c in cases}
        _users(dynamodb_tables).update_item(Key={"email": EMAIL}, UpdateExpression="SET pin_reset_exp = :e",
                                            ExpressionAttributeValues={":e": int(time.time()) - 1})
        msgs.add(json.loads(auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "7395"})["body"])["error"])
        assert msgs == {auth.PIN_RESET_BAD_LINK_MSG}

    def test_weak_pin_does_not_use_up_the_link(self, auth, mail):
        token = self._token(auth, mail)
        assert auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "1234"})["statusCode"] == 400
        assert auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "7395"})["statusCode"] == 200

    def test_newer_request_replaces_older_link(self, auth, mail):
        old = self._token(auth, mail); new = self._token(auth, mail)
        assert auth.reset_pin(_ev(), {"email": EMAIL, "token": old, "new_pin": "7395"})["statusCode"] == 400
        assert auth.reset_pin(_ev(), {"email": EMAIL, "token": new, "new_pin": "7395"})["statusCode"] == 200

    def test_check_link_before_typing_a_pin(self, auth, mail, dynamodb_tables):
        token = self._token(auth, mail)
        r = auth.check_pin_reset(_ev(), {"email": EMAIL, "token": token})
        body = json.loads(r["body"])
        assert r["statusCode"] == 200 and body["valid"] is True
        assert body["expires_in"] == pytest.approx(30 * 60, abs=5)
        # checking uses nothing up: the link still resets afterwards
        assert auth.check_pin_reset(_ev(), {"email": EMAIL, "token": token})["statusCode"] == 200
        assert auth.reset_pin(_ev(), {"email": EMAIL, "token": token, "new_pin": "7395"})["statusCode"] == 200
        used = auth.check_pin_reset(_ev(), {"email": EMAIL, "token": token})
        assert used["statusCode"] == 400 and json.loads(used["body"])["error"] == auth.PIN_RESET_BAD_LINK_MSG

    def test_check_gives_one_answer_for_every_bad_link(self, auth, mail, dynamodb_tables):
        token = self._token(auth, mail)
        cases = [{"email": EMAIL, "token": "wrong"}, {"email": "nobody@example.com", "token": token},
                 {"email": EMAIL, "token": ""}, {"email": "", "token": token}]
        out = {(r["statusCode"], json.loads(r["body"])["error"])
               for r in (auth.check_pin_reset(_ev(), c) for c in cases)}
        _users(dynamodb_tables).update_item(Key={"email": EMAIL}, UpdateExpression="SET pin_reset_exp = :e",
                                            ExpressionAttributeValues={":e": int(time.time()) - 1})
        r = auth.check_pin_reset(_ev(), {"email": EMAIL, "token": token})
        out.add((r["statusCode"], json.loads(r["body"])["error"]))
        assert out == {(400, auth.PIN_RESET_BAD_LINK_MSG)}

    def test_check_is_rate_limited_per_ip(self, auth, mail):
        for _ in range(30):
            auth.check_pin_reset(_ev("8.8.8.8"), {"email": EMAIL, "token": "guess"})
        assert auth.check_pin_reset(_ev("8.8.8.8"), {"email": EMAIL, "token": "guess"})["statusCode"] == 429

    def test_check_route_is_wired(self):
        src = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert '"/auth/check-reset"' in src and "check_pin_reset(event, body)" in src

    def test_admin_never_sees_reset_fields(self, auth, mail, monkeypatch):
        import admin
        self._token(auth, mail)
        monkeypatch.setattr(admin, "get_admin_auth", lambda e: True)
        user = json.loads(admin.admin_get_user({}, EMAIL)["body"])["user"]
        assert not {"pin_hash", "pin_reset_hash", "pin_reset_exp", "verify_token"} & set(user)


class TestFrontend:
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()

    def test_link_checked_before_pin_boxes_show(self):
        body = self.js[self.js.index("function checkResetLink()"):self.js.index("async function submitPinReset()")]
        assert "/auth/check-reset" in body and "verifyResetLink()" in body
        assert body.index("'reset-fields').style.display = 'none'") < body.index("verifyResetLink();")
        assert "_resetExpiryTimer" in body                           # goes dead on screen when it expires
    html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()

    def test_wiring(self):
        assert 'data-action="show-forgot-pin">Forgot PIN?</button>' in self.html
        assert 'id="forgot-form"' in self.html and 'id="reset-form"' in self.html
        assert "if (checkResetLink()) return;" in self.js
        assert "history.replaceState(null, '', location.pathname + location.search)" in self.js   # token leaves the URL
        h = open(os.path.join(_ROOT, "lambda", "handler.py"), encoding="utf-8").read()
        assert '"/auth/forgot-pin"' in h and '"/auth/reset-pin"' in h


# ── Verification reminders ────────────────────────────────────────────────────

@pytest.fixture
def scraper(dynamodb_tables, mail, monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    sys.modules.pop("scraper", None)
    spec = importlib.util.spec_from_file_location("cw_scraper_main911", os.path.join(_ROOT, "scraper", "main.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("scraper", None)


T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _add(ddb, email, verified=False, days_ago=0, **extra):
    item = {"email": email, "created_at": (T0 - timedelta(days=days_ago)).isoformat(), "prefs": {}, **extra}
    if verified is not None:
        item["email_verified"] = verified
    ddb.Table("test-users").put_item(Item=item)


class TestReminders:
    def test_four_weekly_reminders_then_stop(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, EMAIL)
        sent_on = []
        for day in range(0, 60):
            before = len(mail)
            scraper.send_verify_reminders(now=T0 + timedelta(days=day, hours=3))
            if len(mail) > before:
                sent_on.append(day)
        assert sent_on == [7, 14, 21, 28]
        assert [m["subject"] for m in mail][-1] == "Last reminder: verify your Cartwise email"
        row = dynamodb_tables.Table("test-users").get_item(Key={"email": EMAIL})["Item"]
        assert int(row["verify_reminders"]) == 4 and row["verify_token"]

    def test_fresh_link_each_time(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, EMAIL, days_ago=7)
        scraper.send_verify_reminders(now=T0 + timedelta(hours=1))
        row = dynamodb_tables.Table("test-users").get_item(Key={"email": EMAIL})["Item"]
        assert row["verify_token"] in mail[0]["html"] and "/auth/verify-email?" in mail[0]["html"]
        exp = datetime.fromisoformat(row["verify_token_exp"])
        assert exp - (T0 + timedelta(hours=1)) == timedelta(days=7)

    def test_only_unverified_accounts(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, "done@example.com", verified=True, days_ago=10)
        _add(dynamodb_tables, "legacy@example.com", verified=None, days_ago=10)    # no flag = verified (pre-v9)
        _add(dynamodb_tables, "__meta__", days_ago=10)
        _add(dynamodb_tables, "new@example.com", days_ago=3)
        assert scraper.send_verify_reminders(now=T0)["sent"] == 0 and not mail

    def test_old_accounts_catch_up_one_week_at_a_time(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, EMAIL, days_ago=200)
        sent_on = []
        for day in range(0, 30):
            before = len(mail)
            scraper.send_verify_reminders(now=T0 + timedelta(days=day))
            if len(mail) > before:
                sent_on.append(day)
        assert sent_on == [0, 6, 12, 18]

    def test_verifying_stops_reminders(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, EMAIL, days_ago=7)
        scraper.send_verify_reminders(now=T0)
        dynamodb_tables.Table("test-users").update_item(Key={"email": EMAIL}, UpdateExpression="SET email_verified = :t",
                                                        ExpressionAttributeValues={":t": True})
        scraper.send_verify_reminders(now=T0 + timedelta(days=8))
        assert len(mail) == 1

    def test_scheduled(self):
        d = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
        assert 'REMIND_RULE="${APP_NAME}-verify-reminders"' in d and 'cron(0 15 * * ? *)' in d
        assert 'cp "${SCRIPT_DIR}"/shared/*.py "${SCRAPER_TMP}/shared/"' in d
        assert 'cp "${SCRIPT_DIR}"/shared/*.py "${API_BUILD_TMP}/shared/"' in d
        assert '"${APP_NAME}-verify-reminders"' in open(os.path.join(_ROOT, "teardown.sh"), encoding="utf-8").read()


# ── Unsubscribe link in every email ───────────────────────────────────────────

def _unsub_q(link):
    assert "/user/unsubscribe?" in link
    q = parse_qs(link.split("?", 1)[1])
    return q["email"][0], q["token"][0]


def _link_in(msg):
    """The unsubscribe link from an outgoing message: HTML footer, text footer and header must agree."""
    html, text = msg["html"].replace("&amp;", "&"), msg.get("text", "")
    i = html.index("/user/unsubscribe?")
    start = html.rindex('href="', 0, i) + 6
    link = html[start:html.index('"', start)]
    assert ">Unsubscribe from Cartwise emails<" in msg["html"]
    assert f"Unsubscribe from Cartwise emails: {link}" in text
    assert msg["headers"]["List-Unsubscribe"] == f"<{link}>"
    assert msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    return link


def _unsub_ev(email, token, method):
    return {"headers": {}, "cookies": [], "queryStringParameters": {"email": email, "token": token},
            "requestContext": {"http": {"sourceIp": "1.2.3.4", "method": method, "path": "/user/unsubscribe"}}}


class TestUnsubscribeEverywhere:
    def test_all_user_emails_carry_the_link(self, auth, mail, scraper, dynamodb_tables, monkeypatch):
        import prefs
        auth._send_verification_email(EMAIL, "vtok")
        auth.forgot_pin(_ev(), {"email": EMAIL})
        auth._send_pin_changed_email(EMAIL)
        monkeypatch.setattr(prefs, "get_session", lambda e: {"email": EMAIL})
        assert prefs.send_test_email(_ev())["statusCode"] == 200
        _add(dynamodb_tables, "new@example.com", days_ago=7)
        monkeypatch.setattr(scraper, "resend", sys.modules["resend"])
        assert scraper.send_verify_reminders(now=T0)["sent"] == 1
        scraper.send_alert(EMAIL, [], "1", "Test Store", account_email=EMAIL)
        subjects = [m["subject"] for m in mail]
        assert len(mail) == 6, subjects
        for m in mail:
            who, _ = _unsub_q(_link_in(m))
            assert who == (m["to"][0] if m["to"][0] == "new@example.com" else EMAIL), m["subject"]

    def test_link_works_for_an_unverified_account_with_no_prefs(self, auth, mail, scraper, dynamodb_tables):
        import prefs
        _users(dynamodb_tables).put_item(Item={"email": "new@example.com", "email_verified": False,
                                              "created_at": T0.isoformat()})
        auth._send_verification_email("new@example.com", "vtok")
        email, token = _unsub_q(_link_in(mail[-1]))
        page = prefs.unsubscribe(_unsub_ev(email, token, "GET"))
        assert page["statusCode"] == 200 and "Unsubscribe from Cartwise emails?" in page["body"]
        done = prefs.unsubscribe(_unsub_ev(email, token, "POST"))
        assert done["statusCode"] == 200 and "won't get deal alerts or reminders" in done["body"]
        row = _users(dynamodb_tables).get_item(Key={"email": "new@example.com"})["Item"]
        assert row["prefs"]["email_enabled"] is False and row["prefs"]["items"] == [] and row["unsubscribed_at"]
        assert row["email_verified"] is False                          # unsubscribing doesn't verify
        again = prefs.unsubscribe(_unsub_ev(email, token, "GET"))
        assert "Already unsubscribed" in again["body"]

    def test_unsubscribed_accounts_get_no_more_reminders(self, scraper, mail, dynamodb_tables):
        _add(dynamodb_tables, "a@example.com", days_ago=7)
        _add(dynamodb_tables, "b@example.com", days_ago=7, prefs={"email_enabled": False})
        assert scraper.send_verify_reminders(now=T0)["sent"] == 1
        assert [m["to"] for m in mail] == [["a@example.com"]]

    def test_bad_token_still_rejected(self, auth):
        import prefs
        r = prefs.unsubscribe(_unsub_ev(EMAIL, "0" * 64, "POST"))
        assert "This link isn&#x27;t valid" in r["body"]

    def test_one_token_scheme(self):
        import helpers
        from shared import unsub
        assert unsub.token(helpers.UNSUB_SECRET, "Ben@Example.com ") == helpers._unsub_token("ben@example.com")
        assert unsub.url("https://api.x", "changeme-set-in-deploy", EMAIL) == ""

    def test_every_sender_in_the_code_adds_the_link(self):
        """Guard: any module that sends mail to users must use shared/unsub.py.
        feedback.py and report.py (v9.12) are exempt: they only ever email the admin."""
        for rel in ("lambda/auth.py", "lambda/prefs.py", "scraper/main.py"):
            src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
            assert "Emails.send" in src and "from shared import unsub" in src, rel
        for rel in os.listdir(os.path.join(_ROOT, "lambda")):
            if rel.endswith(".py") and rel not in ("auth.py", "prefs.py", "feedback.py", "report.py"):
                assert "Emails.send" not in open(os.path.join(_ROOT, "lambda", rel), encoding="utf-8").read(), rel


# ── Branded email layout ──────────────────────────────────────────────────────

class TestEmailLayout:
    def test_every_email_uses_the_brand_layout(self, auth, mail, scraper, dynamodb_tables, monkeypatch):
        import prefs
        auth._send_verification_email(EMAIL, "vtok")
        auth.forgot_pin(_ev(), {"email": EMAIL})
        auth._send_pin_changed_email(EMAIL)
        monkeypatch.setattr(prefs, "get_session", lambda e: {"email": EMAIL})
        prefs.send_test_email(_ev())
        _add(dynamodb_tables, "new@example.com", days_ago=7)
        monkeypatch.setattr(scraper, "resend", sys.modules["resend"])
        scraper.send_verify_reminders(now=T0)
        scraper.send_alert(EMAIL, [], "1", "Test Store", account_email=EMAIL)
        assert len(mail) == 6
        for m in mail:
            h = m["html"]
            assert "/email-logo.png" in h and 'alt="Cartwise"' in h, m["subject"]       # PNG logo, never SVG
            assert "<svg" not in h and "#1a6b3c" in h and "#faf8f3" in h, m["subject"]  # brand green on cream
            assert 'name="color-scheme" content="light"' in h, m["subject"]
            assert "white-space:nowrap" in h, m["subject"]                              # buttons / links don't wrap
            assert m["text"].rstrip().endswith(("cartwise.shopping", "localhost:3000")), m["subject"]

    def test_email_addresses_only_break_at_the_at_sign(self):
        from shared import email_layout as L
        a = L.addr("long.name+tag@example.com")
        # one link around the whole address (so mail apps can't auto-link half of it),
        # with the only break opportunity just before the @
        assert a.startswith('<a href="mailto:long.name+tag@example.com"') and a.count("<a ") == 1
        assert ">long.name+tag</span><wbr><span" in a
        # what mail apps' auto-linkers scan (the visible text) no longer contains a domain or address
        import re
        visible = re.sub(r"<[^>]+>", "", a)
        assert "example.com" not in visible and "@example" not in visible
        assert visible.replace("&#8204;", "") == "long.name+tag@example.com"     # looks identical
        assert "text-decoration:underline" in a and "#1a6b3c" in a and "&lt;" in L.addr("<x>@y.z")

    def test_logo_is_shipped_and_deployed(self):
        import struct
        raw = open(os.path.join(_ROOT, "frontend", "email-logo.png"), "rb").read(24)
        assert raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR"            # a real PNG
        w, h = struct.unpack(">II", raw[16:24])                                          # no PIL needed
        from shared import email_layout as L
        assert w / h == pytest.approx(L.LOGO_W / L.LOGO_H, rel=0.02)
        assert h >= L.LOGO_H * 3                                                          # sharp on phones
        assert "email-logo.png" in open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()

    def test_landing_pages_are_branded(self, auth):
        import prefs
        for page in (auth._verify_html_response(True, "Your email is verified"),
                     prefs._unsub_confirm_page(EMAIL, "t"), prefs._unsub_html("You're unsubscribed", "x")):
            assert "/email-logo.png" in page["body"] and "#1a6b3c" in page["body"]
        assert ">?</div>" in prefs._unsub_confirm_page(EMAIL, "t")["body"]                # a question, not a ✓
