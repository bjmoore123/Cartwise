"""tests/test_offer.py — v9.3 offer model, email renderer, and v9.4 history prices.

tests/fixtures/publix_offers.json holds one real Publix deal for each of the 55
distinct shapes seen at store 1546 (week of 2026-10-07), with the expected
offer. The explicit tests below pin the cases that were wrong before v9.3.
"""
import json
import os
from datetime import date

import pytest

from shared.deal_parser import classify_offer, parse_deal
from shared import email_render as ER

_FIX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "publix_offers.json"), encoding="utf-8"))


@pytest.mark.parametrize("case", _FIX, ids=[f"{c['raw']['savingType']}:{c['raw']['savings']}" for c in _FIX])
def test_fixture_shapes(case):
    o = parse_deal(case["raw"])["offer"]
    got = {k: o.get(k) for k in case["expect"]}
    assert got == case["expect"]


def test_every_real_shape_is_classified():
    assert not [c for c in _FIX if c["expect"]["kind"] == "other"]


class TestKnownBugs:
    def test_tpr_savings_is_the_discount_not_the_price(self):
        o = classify_offer("Tpr", "$0.24", "", 2.15, 2.39)
        assert (o["kind"], o["headline"], o["context"], o["save_text"], o["pct"]) == \
               ("price", "$2.15", "was $2.39", "Save $0.24", 10)

    def test_tpr_from_old_cache_without_regular_price(self):
        o = classify_offer("Tpr", "$1.30", "", 4.69, 0)
        assert o["regular"] == 5.99 and o["context"] == "was $5.99"

    def test_coupon_is_an_amount_off_not_a_price(self):
        o = classify_offer("DigitalCoupon", "Save $2.00")
        assert (o["kind"], o["headline"], o["price"]) == ("coupon", "$2 off", None)

    def test_bogo_keeps_up_to(self):
        o = classify_offer("WeeklyAd", "Buy 1 Get 1 FREE", "SAVE UP TO $8.99")
        assert o["headline"] == "Buy 1 Get 1 Free"
        assert o["save_text"] == "Save up to $8.99" and o["save_is_max"]
        assert o["context"] == "≈ $4.50 ea when you buy 2"

    def test_b2g1_per_unit(self):
        o = classify_offer("WeeklyAd", "Buy 2 Get 1 FREE", "SAVE UP TO $4.99 ON 3")
        assert o["price"] == 3.33 and o["save"] == 1.66

    def test_multi_buy(self):
        o = classify_offer("WeeklyAd", "2 for $7.00", "SAVE UP TO $2.98 ON 2", 3.5)
        assert (o["headline"], o["context"], o["save_text"], o["save"]) == \
               ("2 for $7", "$3.50 ea", "Save up to $2.98 on 2", 1.49)

    def test_per_lb(self):
        o = classify_offer("WeeklyAd", "$4.99 lb", "SAVE UP TO $3.57 LB", 4.99)
        assert (o["headline"], o["unit"], o["save_text"]) == ("$4.99/lb", "lb", "Save up to $3.57/lb")

    def test_mfr_coupon_flag(self):
        o = classify_offer("WeeklyAd", "$2.00 off WITH MFR DIGITAL COUPON")
        assert o["coupon_required"] and o["headline"] == "$2 off" and "digital coupon" in o["context"]

    def test_flags_not_savings(self):
        o = classify_offer("WeeklyAd", "$7.49 each", "SURPRISINGLY LOW PRICE", 7.49)
        assert o["save_text"] == "" and o["headline"] == "$7.49"

    def test_unknown_text_never_invents_a_price(self):
        o = classify_offer("WeeklyAd", "Mystery deal!")
        assert o["kind"] == "other" and o["headline"] == "Mystery deal!" and o["price"] is None


class TestParseDeal:
    def test_extra_savings_promo_type_4(self):
        d = parse_deal({"id": "1", "title": "X", "savingType": "WeeklyAd", "wa_promotionTypeId": 4, "savings": "$2.00 off"})
        assert d["is_extra"] is True

    def test_stacked_extra_text(self):
        d = parse_deal({"id": "1", "title": "X", "savingType": "Stacked", "savings": "$16.49",
                        "additionalDealInfo": "Save Up To $6.00", "additionalSavings": "Save $1.00", "finalPrice": 16.49})
        assert d["offer"]["extra"] == "+ $1 off with digital coupon"

    def test_offer_is_compact_and_reparse_is_stable(self):
        d = parse_deal({"id": "1", "title": "X", "savingType": "Tpr", "savings": "$0.24", "finalPrice": 2.15, "originalDeal": 2.39})
        assert "flags" not in d["offer"] and "extra" not in d["offer"]
        again = parse_deal(json.loads(json.dumps(d)))          # cache round trip
        assert again["offer"] == d["offer"]

    def test_savings_sort_key_uses_offer(self):
        d = parse_deal({"id": "1", "title": "X", "savingType": "Tpr", "savings": "$1.30", "finalPrice": 4.69})
        assert d["savings_per_item"] == 1.3


class TestEmail:
    def _m(self, **kw):
        base = {"my_item": "milk", "deal_name": "Whole Milk", "savings": "$0.24", "saving_type": "Tpr",
                "offer": classify_offer("Tpr", "$0.24", "", 2.15, 2.39), "valid_from": "10/7", "valid_thru": "10/13"}
        base.update(kw)
        return base

    def test_price_drop_shows_price_not_discount(self):
        html = ER.build_html([self._m()], "1546", "Test Store")
        assert "$2.15" in html and "was $2.39" in html and "Save $0.24 (10%)" in html
        assert ">Price<" not in html

    def test_escapes_titles(self):
        html = ER.build_html([self._m(deal_name="<script>x</script> & Co")], "1546", "<b>")
        assert "<script>x" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html

    def test_unsubscribe_and_text_part(self):
        html = ER.build_html([self._m()], "1546", "S", unsub_url="https://api.x/user/unsubscribe?a=1&b=2")
        assert "unsubscribe?a=1&amp;b=2" in html
        assert "Unsubscribe: https://api.x" in ER.build_text([self._m()], "S", "https://api.x/u")

    def test_tags_match_frontend_taxonomy(self):
        bogo = self._m(saving_type="WeeklyAd", is_bogo=True, offer=classify_offer("WeeklyAd", "Buy 1 Get 1 FREE"))
        multi = self._m(saving_type="WeeklyAd", offer=classify_offer("WeeklyAd", "2 for $7.00", "", 3.5))
        coupon = self._m(saving_type="DigitalCoupon", has_coupon=True, coupon_id="123",
                         offer=classify_offer("DigitalCoupon", "Save $2.00"))
        assert [ER.deal_tag(x) for x in (self._m(), bogo, multi, coupon)] == ["sale", "bogo", "multi", "coupon"]
        assert ER.tag_label(multi, "multi") == "2 for"
        assert "cid=123" in ER.build_html([coupon], "1", "S")

    def test_subject(self):
        assert ER.subject_line([self._m(), self._m()]) == "2 items on your list are on sale (10/7 – 10/13)"
        assert ER.subject_line([{}], date(2026, 10, 12)) == "1 item on your list is on sale (Oct 12)"

    def test_pre_v93_match_without_offer_still_renders(self):
        html = ER.build_html([{"deal_name": "Old", "savings": "$3.99", "save_line": "SAVE $1"}], "1", "S")
        assert "$3.99" in html


class TestHistoryPrices:
    def test_price_from_history_rows(self):
        import history as H
        assert H.deal_price({"saving_type": "WeeklyAd", "savings": "2 for $7.00"}) == 3.5
        assert H.deal_price({"saving_type": "Tpr", "savings": "$0.24"}) is None            # old TPR row
        assert H.deal_price({"saving_type": "Tpr", "savings": "$0.24", "final_price": 2.15}) == 2.15
        assert H.deal_price({"saving_type": "WeeklyAd", "savings": "Buy 1 Get 1 FREE"}) is None

    def test_lowest_price_badge(self):
        import history as H
        weeks = [f"2026-{m:02d}-01" for m in range(10, 2, -1)]          # 8 weeks, newest first
        prices = ["$2.99"] + ["$3.49", "$3.99"] * 3 + ["$3.49"]
        snaps = [{"week": w, "deals": [{"title": "Chips", "savings": p, "saving_type": "WeeklyAd"}]}
                 for w, p in zip(weeks, prices)]
        b = H.compute_badges(H.build_index(snaps))
        assert b["best"] == ["chips"] and "chips" not in b["repeat"]

    def test_not_lowest_when_price_never_changes(self):
        import history as H
        weeks = [f"2026-{m:02d}-01" for m in range(10, 2, -1)]
        snaps = [{"week": w, "deals": [{"title": "Chips", "savings": "$2.99", "saving_type": "WeeklyAd"}]} for w in weeks]
        assert H.compute_badges(H.build_index(snaps))["best"] == []

    def test_old_summary_format_still_loads(self):
        import gzip, history as H
        old = gzip.compress(json.dumps({"weeks": ["2026-10-05"], "titles": {"chips": ["Chips", [[0, "$2.99"]]]}}).encode())
        idx = H.deserialize_index(old)
        assert idx["titles"]["chips"][0] == ("Chips", "$2.99", None)

    def test_points_carry_price(self):
        import history as H
        idx = H.build_index([{"week": "2026-10-05", "deals": [{"title": "Chips", "savings": "$2.99", "saving_type": "WeeklyAd"}]}])
        assert H.title_points(idx, "Chips") == [{"week": "2026-10-05", "savings": "$2.99", "price": 2.99}]
