"""tests/test_matcher.py — Tests for the unified matching engine.

Tests the normalized, brand-aware, composite scorer in shared/matcher.py.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.matcher import match_deals_for_user, _match_score, normalize, tokenize


def _deal(title, id_="1", savings="$2", department="Grocery", brand="", description=""):
    return {"id": id_, "title": title, "savings": savings,
            "description": description, "brand": brand, "department": department,
            "save_line": "", "fine_print": "", "valid_from": "", "valid_thru": "",
            "image_url": ""}


# ── Normalization ─────────────────────────────────────────────────────────────

class TestNormalize:
    def test_strips_special_chars(self):
        assert normalize("Boar's Head® Turkey, Deli-Sliced") == "boars head turkey deli-sliced"

    def test_preserves_hyphens(self):
        assert normalize("Deli-Sliced") == "deli-sliced"

    def test_collapses_whitespace(self):
        assert normalize("  chicken   breast  ") == "chicken breast"

    def test_empty_string(self):
        assert normalize("") == ""

    def test_strips_trademark_symbols(self):
        assert normalize("Publix™ Premium® Juice") == "publix premium juice"


class TestTokenize:
    def test_basic(self):
        assert tokenize("Publix Chicken Breast") == ["publix", "chicken", "breast"]

    def test_with_special_chars(self):
        assert tokenize("Boar's Head®") == ["boars", "head"]


# ── Scoring ───────────────────────────────────────────────────────────────────

class TestMatchScore:
    def test_exact_title_match(self):
        deal = _deal("Publix Whole Milk Gallon")
        score = _match_score("Publix Whole Milk Gallon", deal)
        assert score == 100

    def test_partial_match(self):
        deal = _deal("Publix Whole Milk Gallon")
        score = _match_score("whole milk", deal)
        assert score > 0

    def test_no_match(self):
        deal = _deal("Organic Kale Chips")
        score = _match_score("chocolate cake", deal)
        assert score == 0

    def test_single_word_item(self):
        deal = _deal("Publix Butter Unsalted")
        score = _match_score("butter", deal)
        assert score == 100  # single-word items: presence = 100

    def test_single_word_no_match(self):
        deal = _deal("Publix Chicken Breast")
        score = _match_score("butter", deal)
        assert score == 0

    def test_brand_match_from_field(self):
        """When user's item includes the brand and it matches the deal's brand field."""
        deal = _deal("Ovengold Turkey Breast", brand="Boar's Head")
        score = _match_score("boars head turkey", deal)
        assert score >= 65  # brand match should boost score

    def test_brand_mismatch_penalty(self):
        """When user types a brand that doesn't match the deal's brand."""
        deal = _deal("Ovengold Turkey Breast", brand="Boar's Head")
        score = _match_score("oscar mayer turkey", deal)
        assert score <= 45  # brand mismatch caps score

    def test_description_contributes(self):
        """Words in description should contribute to matching."""
        deal = _deal("Party Wings", description="Publix Chicken Wing Sections")
        score = _match_score("chicken wings", deal)
        assert score > 0

    def test_special_chars_in_title(self):
        """Special chars shouldn't break matching."""
        deal = _deal("Boar's Head® Ovengold™ Turkey Breast, Deli-Sliced")
        score = _match_score("boars head turkey", deal)
        assert score > 50

    def test_noise_words_stripped(self):
        """Noise words (oz, lb, ct) shouldn't affect matching."""
        deal = _deal("Publix Chicken Breast 16 oz")
        score1 = _match_score("chicken breast", deal)
        score2 = _match_score("chicken breast 16 oz", deal)
        assert score1 >= 60
        assert score2 >= 60

    def test_plural_prefix_match(self):
        deal = _deal("Publix Whole Chickens")
        score = _match_score("chicken", deal)
        assert score == 100  # single word, prefix match found

    def test_long_bundle_title_penalized(self):
        """Long bundle titles should score lower via coverage."""
        short_deal = _deal("Publix Chicken Breast Boneless Skinless")
        long_deal = _deal("Any 2 Knorr Pasta Sides AND Any 1 Publix Boneless Skinless Chicken Breast")
        short_score = _match_score("publix chicken breast", short_deal)
        long_score = _match_score("publix chicken breast", long_deal)
        assert short_score > long_score


# ── Integration: match_deals_for_user ─────────────────────────────────────────

class TestMatchDeals:
    def test_basic_match(self):
        deals = [_deal("Publix Whole Milk Gallon")]
        matches = match_deals_for_user(deals, ["whole milk"], {"sensitivity": "normal"})
        assert len(matches) == 1
        assert matches[0]["my_item"] == "whole milk"

    def test_no_match_strict(self):
        deals = [_deal("Organic Kale Chips")]
        matches = match_deals_for_user(deals, ["chocolate cake"], {"sensitivity": "strict"})
        assert len(matches) == 0

    def test_empty_items(self):
        deals = [_deal("Publix Whole Milk")]
        matches = match_deals_for_user(deals, [], {"sensitivity": "normal"})
        assert len(matches) == 0

    def test_empty_deals(self):
        matches = match_deals_for_user([], ["milk"], {"sensitivity": "normal"})
        assert len(matches) == 0

    def test_deduplication(self):
        deals = [_deal("Publix Whole Milk Gallon", id_="42")]
        items = ["whole milk", "milk gallon"]
        matches = match_deals_for_user(deals, items, {"sensitivity": "loose"})
        assert len(matches) == 1

    def test_multiple_deals_multiple_items(self):
        deals = [_deal("Publix Whole Milk", id_="1"), _deal("Chicken Breast", id_="2")]
        items = ["milk", "chicken"]
        matches = match_deals_for_user(deals, items, {"sensitivity": "loose"})
        assert len(matches) == 2

    def test_default_sensitivity(self):
        deals = [_deal("Publix Milk")]
        matches = match_deals_for_user(deals, ["milk"], {})
        assert isinstance(matches, list)

    def test_sensitivity_strict_fewer_matches(self):
        deals = [
            _deal("Publix Chicken Breast", id_="1"),
            _deal("Any 2 Knorr Sides AND Publix Chicken", id_="2"),
        ]
        strict = match_deals_for_user(deals, ["publix chicken"], {"sensitivity": "strict"})
        loose = match_deals_for_user(deals, ["publix chicken"], {"sensitivity": "loose"})
        assert len(strict) <= len(loose)

    def test_legacy_dict_items(self):
        deals = [_deal("Publix Orange Juice")]
        matches = match_deals_for_user(deals, [{"name": "orange juice", "mode": "fuzzy"}], {"sensitivity": "normal"})
        assert len(matches) == 1
        assert matches[0]["my_item"] == "orange juice"

    def test_plain_string_items(self):
        deals = [_deal("Publix Butter Unsalted")]
        matches = match_deals_for_user(deals, ["butter"], {"sensitivity": "normal"})
        assert len(matches) == 1

    def test_brand_in_deal_field(self):
        deals = [_deal("Ovengold Turkey Breast", brand="Boar's Head", id_="1")]
        matches = match_deals_for_user(deals, ["boars head turkey"], {"sensitivity": "normal"})
        assert len(matches) == 1

    def test_sorted_by_score(self):
        deals = [
            _deal("Publix Chicken Breast Boneless Skinless", id_="1"),
            _deal("Publix Chicken", id_="2"),
        ]
        matches = match_deals_for_user(deals, ["publix chicken breast"], {"sensitivity": "loose"})
        if len(matches) >= 2:
            assert matches[0]["match_score"] >= matches[1]["match_score"]
