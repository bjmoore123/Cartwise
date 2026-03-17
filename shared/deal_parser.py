"""shared/deal_parser.py — Unified deal parsing for both Lambdas.

Used by:
  - lambda/deals.py   (API Lambda)
  - scraper/scraper.py (Scraper Lambda)

Both Lambdas include the shared/ directory in their deployment packages.
"""
import html as _html
import re

# Comprehensive BOGO regex — covers b1g1, b2g1, "buy one get one", "buy 2 get 1", etc.
_BOGO_RE = re.compile(r"b[12]g1|buy\s+\d.{0,8}get\s+\d|buy\s+one.{0,10}get\s+one", re.IGNORECASE)

# Per-item savings regex — "SAVE UP TO $7.39 ON 3" → $2.46/item
_PER_ITEM_RE = re.compile(r'\$(\d+(?:\.\d+)?)\s+on\s+(\d+)', re.IGNORECASE)


def _clean(s) -> str:
    """Unescape HTML entities from Publix API responses.

    The Publix API returns HTML-encoded strings inside JSON, e.g.
    "Spindrift Sparkling Water &amp; Real Squeezed Fruit". Python's
    json.loads() preserves these as literal &amp; characters. This
    function converts them to their proper Unicode equivalents at the
    point of ingestion so entities never reach DynamoDB or API responses.
    """
    return _html.unescape(s) if s else ""


def parse_per_item_savings(save_line: str) -> float | None:
    """Extract per-item savings from bundle save lines.

    'SAVE UP TO $7.39 ON 3' → 2.46
    'SAVE UP TO $10.98 ON 2' → 5.49
    Returns None if no parseable bundle pattern found.
    """
    if not save_line:
        return None
    m = _PER_ITEM_RE.search(save_line)
    if not m:
        return None
    total = float(m.group(1))
    qty   = int(m.group(2))
    return round(total / qty, 2) if qty > 0 else total


def parse_deal(d: dict) -> dict:
    """Parse and enrich a deal dict from either a raw Publix API response or a cached record.

    Accepts both camelCase (raw API) and snake_case (cached) field names.
    Returns a standardised dict with all computed boolean fields.

    All string fields are run through _clean() to unescape HTML entities
    at ingestion time — the Publix API embeds HTML entities in JSON values.
    """
    saving_type = d.get("saving_type") or d.get("savingType") or "WeeklyAd"

    # Clean all string fields at ingestion — entities never propagate further
    title      = _clean(d.get("title", ""))
    description= _clean(d.get("description", ""))
    savings    = _clean(d.get("savings", ""))
    save_line  = _clean(d.get("save_line") or d.get("additionalDealInfo", ""))
    fine_print = _clean(d.get("finePrint") or d.get("fine_print", ""))
    brand      = _clean(d.get("brand", ""))
    department = _clean(d.get("department", ""))

    brand_lower  = brand.lower()
    title_lower  = title.lower()
    savings_str  = savings.lower()
    categories   = d.get("categories") or []

    bogo_text = title_lower + " " + savings_str
    is_bogo   = "bogo" in categories or bool(_BOGO_RE.search(bogo_text))

    # Coupon ID — prefer dcId (raw) then coupon_id (cached)
    coupon_raw = d.get("dcId") or d.get("coupon_id")
    coupon_id  = str(coupon_raw) if coupon_raw else ""

    # Pre-compute per-item savings for sort — stored as a number so the
    # frontend sort doesn't need to parse save_line text at runtime.
    savings_per_item = parse_per_item_savings(save_line)

    # final_price: numeric sale price from the API (0 = no individual price, e.g. BOGO)
    try:
        final_price = float(d.get("final_price") or d.get("finalPrice") or 0)
    except (TypeError, ValueError):
        final_price = 0.0

    # wa_promotion_type_id: 1=Weekly Ad, 4=Extra Savings
    wa_promo_id = d.get("wa_promotion_type_id") or d.get("wa_promotionTypeId") or 1

    return {
        "id":                   str(d.get("id", "")),
        "title":                title,
        "description":          description,
        "savings":              savings,
        "save_line":            save_line,
        "fine_print":           fine_print,
        "brand":                brand,
        "department":           department,
        "valid_from":           d.get("valid_from") or d.get("wa_startDateFormatted", ""),
        "valid_thru":           d.get("valid_thru") or d.get("wa_endDateFormatted", ""),
        "image_url":            d.get("image_url") or d.get("enhancedImageUrl") or d.get("imageUrl", ""),
        "saving_type":          saving_type,
        "coupon_id":            coupon_id,
        "categories":           categories,
        "savings_per_item":     savings_per_item,     # float or None; used by frontend sort
        "final_price":          final_price,           # numeric sale price; 0 if not applicable
        "wa_promotion_type_id": wa_promo_id,           # 1=WeeklyAd, 4=ExtraSavings
        "is_bogo":              is_bogo,
        "is_publix_brand":      brand_lower == "publix" or title_lower.startswith("publix "),
        "has_coupon":           bool(d.get("has_coupon") or d.get("hasCoupon")) or saving_type in ("PrintableCoupon", "DigitalCoupon"),
        "is_stacked":           saving_type == "StackedDeals",
        "is_extra":             saving_type == "ExtraSavings",
    }
