"""tests/test_deal_parser.py — Tests for shared deal parsing logic.

Architecture #2: ensures both Lambdas share the same BOGO detection logic
and produce consistent field shapes.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from shared.deal_parser import parse_deal, parse_per_item_savings, parse_per_item_savings


class TestBogoDetection:
    def test_bogo_in_categories(self):
        result = parse_deal({"title": "Chips", "categories": ["bogo"], "savings": "$3"})
        assert result["is_bogo"] is True

    def test_b1g1_in_title(self):
        result = parse_deal({"title": "B1G1 Bread", "categories": [], "savings": ""})
        assert result["is_bogo"] is True

    def test_b2g1_in_title(self):
        result = parse_deal({"title": "B2G1 Soda Free", "categories": [], "savings": ""})
        assert result["is_bogo"] is True

    def test_buy_2_get_1_in_savings(self):
        result = parse_deal({"title": "Cereal", "savings": "Buy 2 Get 1 Free", "categories": []})
        assert result["is_bogo"] is True

    def test_buy_one_get_one_in_savings(self):
        result = parse_deal({"title": "Yogurt", "savings": "Buy One Get One Free", "categories": []})
        assert result["is_bogo"] is True

    def test_buy_1_get_1_in_savings(self):
        result = parse_deal({"title": "Juice", "savings": "Buy 1 Get 1 Free", "categories": []})
        assert result["is_bogo"] is True

    def test_regular_deal_not_bogo(self):
        result = parse_deal({"title": "Milk", "savings": "$2.50", "categories": ["dairy"]})
        assert result["is_bogo"] is False

    def test_empty_deal_not_bogo(self):
        result = parse_deal({})
        assert result["is_bogo"] is False


class TestHtmlEntityDecoding:
    """Verify that HTML entities from the Publix API are decoded at ingestion."""

    def test_ampersand_in_title(self):
        result = parse_deal({"title": "Spindrift Sparkling Water &amp; Real Squeezed Fruit"})
        assert result["title"] == "Spindrift Sparkling Water & Real Squeezed Fruit"
        assert "&amp;" not in result["title"]

    def test_ampersand_in_description(self):
        result = parse_deal({"description": "Chicken &amp; Rice"})
        assert result["description"] == "Chicken & Rice"

    def test_ampersand_in_savings(self):
        result = parse_deal({"savings": "Save $2 on 2 &amp; more"})
        assert "&amp;" not in result["savings"]

    def test_other_entities_decoded(self):
        result = parse_deal({"title": "Kraft Mac &amp; Cheese &lt;12oz&gt;"})
        assert result["title"] == "Kraft Mac & Cheese <12oz>"

    def test_already_clean_strings_unchanged(self):
        result = parse_deal({"title": "Publix Whole Milk", "savings": "$1.50 off"})
        assert result["title"] == "Publix Whole Milk"
        assert result["savings"] == "$1.50 off"

    def test_empty_string_safe(self):
        result = parse_deal({"title": ""})
        assert result["title"] == ""

    def test_none_fields_safe(self):
        result = parse_deal({"title": None, "savings": None})
        assert result["title"] == ""
        assert result["savings"] == ""


class TestPerItemSavings:
    """Verify parse_per_item_savings and savings_per_item field in parse_deal."""

    def test_bundle_three(self):
        assert parse_per_item_savings("SAVE UP TO $7.39 ON 3") == pytest.approx(2.46, rel=1e-2)

    def test_bundle_two(self):
        assert parse_per_item_savings("SAVE UP TO $10.98 ON 2") == pytest.approx(5.49, rel=1e-2)

    def test_case_insensitive(self):
        assert parse_per_item_savings("save up to $6.00 on 3") == pytest.approx(2.0)

    def test_no_bundle_returns_none(self):
        assert parse_per_item_savings("$2.50 off") is None

    def test_empty_returns_none(self):
        assert parse_per_item_savings("") is None

    def test_none_returns_none(self):
        assert parse_per_item_savings(None) is None

    def test_parse_deal_stores_savings_per_item(self):
        result = parse_deal({"save_line": "SAVE UP TO $7.39 ON 3"})
        assert result["savings_per_item"] == pytest.approx(2.46, rel=1e-2)

    def test_parse_deal_none_when_no_bundle(self):
        result = parse_deal({"save_line": "$2.00 off", "savings": "$2.00"})
        assert result["savings_per_item"] is None

    def test_parse_deal_additionalDealInfo_fallback(self):
        """Raw Publix API uses additionalDealInfo for save_line."""
        result = parse_deal({"additionalDealInfo": "SAVE UP TO $10.98 ON 2"})
        assert result["savings_per_item"] == pytest.approx(5.49, rel=1e-2)


class TestSavingType:
    def test_default_saving_type(self):
        result = parse_deal({"title": "Test"})
        assert result["saving_type"] == "WeeklyAd"

    def test_camel_case_saving_type(self):
        result = parse_deal({"title": "Test", "savingType": "ExtraSavings"})
        assert result["saving_type"] == "ExtraSavings"
        assert result["is_extra"] is True
        assert result["is_stacked"] is False

    def test_snake_case_saving_type(self):
        result = parse_deal({"title": "Test", "saving_type": "StackedDeals"})
        assert result["is_stacked"] is True

    def test_coupon_detection_from_saving_type(self):
        result = parse_deal({"title": "Test", "savingType": "DigitalCoupon"})
        assert result["has_coupon"] is True

    def test_printable_coupon(self):
        result = parse_deal({"title": "Test", "savingType": "PrintableCoupon"})
        assert result["has_coupon"] is True

    def test_weekly_ad_not_coupon(self):
        result = parse_deal({"title": "Test", "savingType": "WeeklyAd"})
        assert result["has_coupon"] is False


class TestPublixBrand:
    def test_publix_brand_field(self):
        result = parse_deal({"title": "Milk", "brand": "Publix"})
        assert result["is_publix_brand"] is True

    def test_publix_in_title(self):
        result = parse_deal({"title": "Publix Whole Milk"})
        assert result["is_publix_brand"] is True

    def test_non_publix_brand(self):
        result = parse_deal({"title": "Cheerios", "brand": "General Mills"})
        assert result["is_publix_brand"] is False


class TestFieldExtraction:
    def test_raw_api_fields(self):
        """Handles camelCase fields from the raw Publix API."""
        deal = {
            "id": "123",
            "title": "Test Item",
            "savingType": "WeeklyAd",
            "additionalDealInfo": "Limit 4",
            "finePrint": "While supplies last",
            "wa_startDateFormatted": "2025-01-01",
            "wa_endDateFormatted": "2025-01-07",
            "enhancedImageUrl": "https://img.publix.com/test.jpg",
            "dcId": "456",
        }
        result = parse_deal(deal)
        assert result["id"] == "123"
        assert result["save_line"] == "Limit 4"
        assert result["fine_print"] == "While supplies last"
        assert result["valid_from"] == "2025-01-01"
        assert result["valid_thru"] == "2025-01-07"
        assert result["image_url"] == "https://img.publix.com/test.jpg"
        assert result["coupon_id"] == "456"

    def test_cached_snake_case_fields(self):
        """Handles snake_case fields from the DynamoDB cache."""
        deal = {
            "id": "789",
            "title": "Cached Item",
            "saving_type": "ExtraSavings",
            "save_line": "Save $2",
            "fine_print": "See label",
            "valid_from": "2025-02-01",
            "valid_thru": "2025-02-07",
            "image_url": "https://img.publix.com/cached.jpg",
            "coupon_id": "101",
        }
        result = parse_deal(deal)
        assert result["id"] == "789"
        assert result["save_line"] == "Save $2"
        assert result["coupon_id"] == "101"
        assert result["is_extra"] is True

    def test_empty_deal_all_fields_present(self):
        """parse_deal always returns all expected keys, even for empty input."""
        result = parse_deal({})
        required_keys = {
            "id", "title", "description", "savings", "save_line", "fine_print",
            "brand", "department", "valid_from", "valid_thru", "image_url",
            "saving_type", "coupon_id", "categories", "savings_per_item",
            "is_bogo", "is_publix_brand", "has_coupon", "is_stacked", "is_extra",
        }
        assert required_keys.issubset(result.keys())
        assert result["id"] == ""
        assert result["is_bogo"] is False
        assert result["categories"] == []
        assert result["savings_per_item"] is None



class TestBogoDetection:
    def test_bogo_in_categories(self):
        result = parse_deal({"title": "Chips", "categories": ["bogo"], "savings": "$3"})
        assert result["is_bogo"] is True

    def test_b1g1_in_title(self):
        result = parse_deal({"title": "B1G1 Bread", "categories": [], "savings": ""})
        assert result["is_bogo"] is True

    def test_b2g1_in_title(self):
        result = parse_deal({"title": "B2G1 Soda Free", "categories": [], "savings": ""})
        assert result["is_bogo"] is True

    def test_buy_2_get_1_in_savings(self):
        result = parse_deal({"title": "Cereal", "savings": "Buy 2 Get 1 Free", "categories": []})
        assert result["is_bogo"] is True

    def test_buy_one_get_one_in_savings(self):
        result = parse_deal({"title": "Yogurt", "savings": "Buy One Get One Free", "categories": []})
        assert result["is_bogo"] is True

    def test_buy_1_get_1_in_savings(self):
        result = parse_deal({"title": "Juice", "savings": "Buy 1 Get 1 Free", "categories": []})
        assert result["is_bogo"] is True

    def test_regular_deal_not_bogo(self):
        result = parse_deal({"title": "Milk", "savings": "$2.50", "categories": ["dairy"]})
        assert result["is_bogo"] is False

    def test_empty_deal_not_bogo(self):
        result = parse_deal({})
        assert result["is_bogo"] is False


class TestSavingType:
    def test_default_saving_type(self):
        result = parse_deal({"title": "Test"})
        assert result["saving_type"] == "WeeklyAd"

    def test_camel_case_saving_type(self):
        result = parse_deal({"title": "Test", "savingType": "ExtraSavings"})
        assert result["saving_type"] == "ExtraSavings"
        assert result["is_extra"] is True
        assert result["is_stacked"] is False

    def test_snake_case_saving_type(self):
        result = parse_deal({"title": "Test", "saving_type": "StackedDeals"})
        assert result["is_stacked"] is True

    def test_coupon_detection_from_saving_type(self):
        result = parse_deal({"title": "Test", "savingType": "DigitalCoupon"})
        assert result["has_coupon"] is True

    def test_printable_coupon(self):
        result = parse_deal({"title": "Test", "savingType": "PrintableCoupon"})
        assert result["has_coupon"] is True

    def test_weekly_ad_not_coupon(self):
        result = parse_deal({"title": "Test", "savingType": "WeeklyAd"})
        assert result["has_coupon"] is False


class TestPublixBrand:
    def test_publix_brand_field(self):
        result = parse_deal({"title": "Milk", "brand": "Publix"})
        assert result["is_publix_brand"] is True

    def test_publix_in_title(self):
        result = parse_deal({"title": "Publix Whole Milk"})
        assert result["is_publix_brand"] is True

    def test_non_publix_brand(self):
        result = parse_deal({"title": "Cheerios", "brand": "General Mills"})
        assert result["is_publix_brand"] is False


class TestFieldExtraction:
    def test_raw_api_fields(self):
        """Handles camelCase fields from the raw Publix API."""
        deal = {
            "id": "123",
            "title": "Test Item",
            "savingType": "WeeklyAd",
            "additionalDealInfo": "Limit 4",
            "finePrint": "While supplies last",
            "wa_startDateFormatted": "2025-01-01",
            "wa_endDateFormatted": "2025-01-07",
            "enhancedImageUrl": "https://img.publix.com/test.jpg",
            "dcId": "456",
        }
        result = parse_deal(deal)
        assert result["id"] == "123"
        assert result["save_line"] == "Limit 4"
        assert result["fine_print"] == "While supplies last"
        assert result["valid_from"] == "2025-01-01"
        assert result["valid_thru"] == "2025-01-07"
        assert result["image_url"] == "https://img.publix.com/test.jpg"
        assert result["coupon_id"] == "456"

    def test_cached_snake_case_fields(self):
        """Handles snake_case fields from the DynamoDB cache."""
        deal = {
            "id": "789",
            "title": "Cached Item",
            "saving_type": "ExtraSavings",
            "save_line": "Save $2",
            "fine_print": "See label",
            "valid_from": "2025-02-01",
            "valid_thru": "2025-02-07",
            "image_url": "https://img.publix.com/cached.jpg",
            "coupon_id": "101",
        }
        result = parse_deal(deal)
        assert result["id"] == "789"
        assert result["save_line"] == "Save $2"
        assert result["coupon_id"] == "101"
        assert result["is_extra"] is True

    def test_empty_deal_all_fields_present(self):
        """parse_deal always returns all expected keys, even for empty input."""
        result = parse_deal({})
        required_keys = {
            "id", "title", "description", "savings", "save_line", "fine_print",
            "brand", "department", "valid_from", "valid_thru", "image_url",
            "saving_type", "coupon_id", "categories",
            "is_bogo", "is_publix_brand", "has_coupon", "is_stacked", "is_extra",
        }
        assert required_keys.issubset(result.keys())
        assert result["id"] == ""
        assert result["is_bogo"] is False
        assert result["categories"] == []
