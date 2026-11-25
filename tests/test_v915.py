"""v9.15: Publix runs two ad cycles (Wed–Tue and Thu–Wed). The weekly run fires Wednesday and
Thursday; each store emails once, on the run when its new ad has just started."""
import importlib.util, os, sys, types
from datetime import datetime, timezone

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
WED = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)      # 7 AM Eastern, Wednesday
THU = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


def deals(valid_from, n=5):
    return [{"id": f"d{i}", "title": "Milk", "saving_type": "WeeklyAd", "valid_from": valid_from,
             "valid_thru": "12/1"} for i in range(n)] + [{"id": "tpr", "saving_type": "Tpr", "valid_from": "1/1"}]


@pytest.fixture
def s(dynamodb_tables, monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key="", Emails=types.SimpleNamespace(send=sent.append)))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.syspath_prepend(os.path.join(_ROOT, "scraper"))
    spec = importlib.util.spec_from_file_location("cw_scraper_main915", os.path.join(_ROOT, "scraper", "main.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m._sent = sent
    return m


class TestWhichRunSends:
    def test_wednesday_store(self, s):
        assert s._should_send_ad("1", deals("11/25"), WED)[0] is True
        go, why = s._should_send_ad("1", deals("11/25"), THU)               # Thursday: same ad, already sent
        assert go is False and "already emailed" in why

    def test_thursday_store(self, s):
        go, why = s._should_send_ad("2", deals("11/19"), WED)               # last week's ad, ends tonight
        assert go is False and "Thursday" in why and "next ad" in why
        assert s._should_send_ad("2", deals("11/26"), THU)[0] is True
        assert s._should_send_ad("2", deals("11/26"), THU)[0] is False      # a retried run can't send twice

    def test_wednesday_failure_is_retried_thursday(self, s):
        assert s._should_send_ad("3", deals("11/25"), THU)[0] is True       # Wednesday skipped (Publix down)

    def test_unknown_start_falls_back_to_wednesday_once(self, s):
        none = [{"id": "x", "saving_type": "WeeklyAd", "valid_from": ""}]
        assert s._should_send_ad("4", none, THU)[0] is False
        assert s._should_send_ad("4", none, WED)[0] is True
        assert s._should_send_ad("4", none, WED)[0] is False

    def test_eastern_date_not_utc(self, s):
        late_tue = datetime(2026, 11, 25, 3, tzinfo=timezone.utc)           # 10 PM Tuesday Eastern
        assert s._should_send_ad("5", deals("11/25"), late_tue)[0] is False


class TestWorker:
    def _user(self):
        return {"email": "a@x.com", "email_verified": True,
                "prefs": {"store_id": "9", "items": ["milk"], "email_enabled": True}}

    def test_skipped_store_sends_nothing_and_records_no_run(self, s, monkeypatch, dynamodb_tables):
        monkeypatch.setattr(s, "fetch_deals_safely", lambda sid: (deals("11/19"), ""))
        monkeypatch.setattr(s, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: WED)}))
        sent = []
        monkeypatch.setattr(s, "send_alert", lambda *a, **k: sent.append(a))
        s._store_worker({"store_id": "9", "users": [self._user()], "send_emails": True, "job_id": "20261125-120000"})
        assert sent == []
        assert "Item" not in dynamodb_tables.Table("test-metrics").get_item(Key={"pk": "run#2026-11-25"})

    def test_one_user_resend_always_sends(self, s, monkeypatch):
        monkeypatch.setattr(s, "fetch_deals_safely", lambda sid: (deals("11/19"), ""))
        monkeypatch.setattr(s, "match_deals_for_user", lambda d, i, m: [{"my_item": "milk"}])
        sent = []
        monkeypatch.setattr(s, "send_alert", lambda *a, **k: sent.append(a))
        s._store_worker({"store_id": "9", "users": [self._user()], "send_emails": True})   # no job_id
        assert len(sent) == 1

    def test_thursday_run_adds_to_wednesdays_row(self, s, dynamodb_tables):
        assert s._run_day("20261125-120000") == "2026-11-25"
        assert s._run_day("20261126-120000") == "2026-11-25"
        base = {"users_with_list": 1, "users_matched": 1, "matches_total": 2, "emails_sent": 1,
                "skipped_unverified": 0, "skipped_opted_out": 0, "send_failed": 0}
        s._record_run_summary("20261125-120000", base, {"milk": 1})
        s._record_run_summary("20261126-120000", base, {"milk": 1})
        run = dynamodb_tables.Table("test-metrics").get_item(Key={"pk": "run#2026-11-25"})["Item"]
        assert int(run["stores"]) == 2 and int(run["emails_sent"]) == 2


def test_schedule_runs_every_morning():
    # v9.15.1: daily, so a holiday-shifted ad (e.g. Thanksgiving week) is still caught the day it starts
    src = open(os.path.join(_ROOT, "deploy.sh"), encoding="utf-8").read()
    assert 'cron(0 12 * * ? *)' in src and 'cron(0 12 ? * WED' not in src


class TestHolidayWeek:
    """Thanksgiving 2026 as Publix actually ran it: Wednesday stores' ad 11/18 ran an extra day."""
    def test_wednesday_nothing_new(self, s):
        assert s._should_send_ad("w", deals("11/18"), WED)[0] is False      # Wednesday store, old ad
        assert s._should_send_ad("t", deals("11/19"), WED)[0] is False      # Thursday store, old ad

    def test_new_ad_any_weekday(self, s):
        fri = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        assert s._should_send_ad("w", deals("11/27"), fri)[0] is True       # ad pushed to Friday
        sat = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
        assert s._should_send_ad("w", deals("11/27"), sat)[0] is False      # already sent
        assert s._should_send_ad("w", deals("11/27"), datetime(2026, 11, 30, 12, tzinfo=timezone.utc))[0] is False

    def test_run_rows_group_by_ad_week(self, s):
        for d in ("20261125", "20261126", "20261127", "20261130", "20261201"):
            assert s._run_day(d + "-120000") == "2026-11-25"
        assert s._run_day("20261202-120000") == "2026-12-02"


def test_notice_popup_once_and_only_for_existing_alert_users():
    js = open(os.path.join(_ROOT, "frontend", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(_ROOT, "frontend", "index.html"), encoding="utf-8").read()
    assert 'id="adday-ov"' in html and "Your deal alert day may change" in html
    assert "adDayNoticeMaybe(wa);" in js                       # shown once deals (and their ad dates) load
    assert "if(isFirstLogin) _adDaySeen(true);" in js           # brand-new accounts never see it
    assert "prefs.email_enabled === false" in js                # not for people with alerts off
    assert "if(o.id==='adday-ov') _adDaySeen(true);" in js      # clicking outside also dismisses for good
    assert "action === 'adday-ok'" in js and "APP_VERSION = '9.15'" in js


class TestAdDatesInEmail:
    """v9.15.1: the alert shows the store's ad dates (from all its deals), not the first match's."""
    def _deals(self):
        return ([{"id": f"a{i}", "title": "Milk", "saving_type": "WeeklyAd", "valid_from": "11/26", "valid_thru": "12/2"} for i in range(6)]
                + [{"id": "short", "title": "Pie", "saving_type": "WeeklyAd", "valid_from": "11/26", "valid_thru": "11/27"},
                   {"id": "cpn", "title": "Coffee", "saving_type": "DigitalCoupon", "valid_from": "11/1", "valid_thru": "12/31"}])

    def test_window(self):
        sys.path.insert(0, _ROOT)
        from datetime import date
        from shared import adweek
        win = adweek.window(self._deals(), date(2026, 11, 26))
        assert win == (date(2026, 11, 26), date(2026, 12, 2))
        assert adweek.span(win) == "Thu 11/26 – Wed 12/2"
        assert adweek.window([{"saving_type": "Tpr", "valid_from": "1/1", "valid_thru": "1/1"}], date(2026, 11, 26)) is None
        assert adweek.parse("1/2", date(2026, 12, 30)) == date(2027, 1, 2)          # Dec→Jan

    def test_subject_header_and_rows(self):
        sys.path.insert(0, _ROOT)
        from datetime import date
        from shared import adweek, email_render
        d = self._deals()
        win = adweek.window(d, date(2026, 11, 26))
        m = [dict(x, my_item="pie" if x["id"] == "short" else "milk", deal_name=x["title"], savings="$1") for x in (d[0], d[6])]
        # the first match is NOT what decides the dates any more
        assert email_render.subject_line(list(reversed(m)), ad=win).endswith("(Thu 11/26 – Wed 12/2)")
        html = email_render.build_html(m, "1", "Publix", ad=win)
        assert "Weekly ad Thu 11/26 – Wed 12/2" in html
        assert html.count("Valid 11/26 – 11/27") == 1 and "Valid 11/26 – 12/2" not in html   # only the exception repeats
        txt = email_render.build_text(m, "Publix", ad=win)
        assert "Weekly ad: Thu 11/26 – Wed 12/2." in txt and "valid 11/26 – 11/27" in txt
        # without ad dates (old callers) it falls back to the first match, as before
        assert "Valid 11/26 – 12/2" in email_render.build_html(m, "1", "Publix")

    def test_worker_passes_ad_dates(self, s, monkeypatch):
        monkeypatch.setattr(s, "fetch_deals_safely", lambda sid: (self._deals(), ""))
        monkeypatch.setattr(s, "match_deals_for_user", lambda d, i, m: [dict(d[0], my_item="milk")])
        got = []
        monkeypatch.setattr(s, "send_alert", lambda *a, **k: got.append(k.get("ad")))
        s._store_worker({"store_id": "9", "send_emails": True,
                         "users": [{"email": "a@x.com", "prefs": {"store_id": "9", "items": ["milk"]}}]})
        assert got and got[0] is not None and adweek_span(got[0]) == "Thu 11/26 – Wed 12/2"


def adweek_span(win):
    sys.path.insert(0, _ROOT)
    from shared import adweek
    return adweek.span(win)
