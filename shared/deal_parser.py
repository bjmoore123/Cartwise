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

# Multi-buy price regex — "2 for $25.99" or "2/$25.99" → $12.995/unit
_MULTI_BUY_RE = re.compile(r'(\d+)\s*(?:for|/)\s*\$(\d+(?:\.\d+)?)', re.IGNORECASE)


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


def parse_multi_buy_unit_price(text: str) -> float | None:
    """Extract per-unit price from multi-buy pricing strings.

    '2 for $25.99' → 13.00
    '3/$10.00'     → 3.33
    Returns None if no multi-buy pattern found.
    """
    if not text:
        return None
    m = _MULTI_BUY_RE.search(text)
    if not m:
        return None
    qty   = int(m.group(1))
    total = float(m.group(2))
    return round(total / qty, 2) if qty > 0 else total


# ── Offer model (v9.3) ────────────────────────────────────────────────────────
# Publix packs very different things into `savings`: a sale price ("$2.15"),
# a multi-buy ("2 for $7.00"), a mechanic ("Buy 1 Get 1 FREE"), a discount
# ("Save $2.00", "$1.00 off") and, for TPR deals, the DISCOUNT AMOUNT (the sale
# price is `finalPrice`, the regular price `originalDeal`). classify_offer()
# turns all of that into one structure so every screen and the email render
# the same thing: headline (what you pay or the mechanic), context, save_text.
#
#   kind      price | multi | bogo | amount_off | pct_off | coupon | free | other
#   headline  "$2.15", "$4.99/lb", "2 for $7", "Buy 1 Get 1 Free", "$2 off"
#   context   "was $2.39", "$3.50 ea", "≈ $2.15 ea", "digital coupon", ...
#   save_text "Save $0.24", "Save up to $3.57/lb", "Save up to $2.98 on 2"
#   price     effective price per unit you pay (float) or None
#   regular   regular price per unit or None
#   save      savings per unit (float) or None; save_is_max when "up to"
#   pct       savings percent (int) or None
#   unit      "ea" | "lb"
#   coupon_required  True when a (manufacturer) digital coupon must be clipped
#   extra     stacked extra coupon text, e.g. "+ $1 off with digital coupon"
#   flags     ["your choice", "new", "low price", "limited time"]
# Rendered by savingsDisplay() in frontend/app.js and shared/email_render.py.

_MONEY = r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)"
_RE_PRICE      = re.compile(r"^(?:sale price\s+|from\s+)?" + _MONEY + r"\s*(lb|each|ea)?\.?$", re.I)
_RE_MULTI      = re.compile(r"^(\d+)\s*(?:for|/)\s*" + _MONEY + r"$", re.I)
_RE_BOGO       = re.compile(r"^buy\s+(?:any\s+)?(\d+|one)\s+(?:and\s+|&\s+)?get\s+(?:any\s+)?(\d+|one)\s+free", re.I)
_RE_BUY_OFF    = re.compile(r"^buy\s+(\d+)\s+get\s+" + _MONEY + r"\s+off", re.I)
_RE_OFF        = re.compile(r"^" + _MONEY + r"\s+off(?:\s+" + _MONEY + r")?", re.I)
_RE_SAVE       = re.compile(r"^save\s+" + _MONEY, re.I)
_RE_PCT        = re.compile(r"^(\d+)\s*%\s*off", re.I)
_RE_SAVE_LINE  = re.compile(r"save\s+(up\s+to\s+)?" + _MONEY + r"(?:\s*(lb))?(?:\s+on\s+(\d+))?", re.I)
_FLAG_WORDS    = (("your choice", "your choice"), ("new", "new"),
                  ("surprisingly low price", "low price"), ("limited time", "limited time"))


def _money(x) -> float:
    return float(str(x).replace(",", ""))


def _fmt(x: float) -> str:
    """$7 / $2.15 — drop .00 for whole dollars."""
    return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def _fmt2(x: float) -> str:
    """Prices always show cents: $5.00, $2.15."""
    return f"${x:,.2f}"


def _num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_save_line(line: str):
    """'SAVE UP TO $2.98 ON 2' -> (2.98, True, 2, None); flags from the rest."""
    flags = []
    low = (line or "").lower()
    for needle, flag in _FLAG_WORDS:
        if re.search(r"\b" + needle + r"\b", low):
            flags.append(flag)
    m = _RE_SAVE_LINE.search(line or "")
    if not m:
        return None, False, None, None, flags
    return _money(m.group(2)), bool(m.group(1)), int(m.group(4)) if m.group(4) else None, \
        ("lb" if m.group(3) else None), flags


def _qty(word: str) -> int:
    return 1 if word.lower() == "one" else int(word)


def classify_offer(saving_type: str, savings: str, save_line: str = "",
                   final_price: float = 0, regular_price: float = 0,
                   additional_savings: str = "") -> dict:
    st   = (saving_type or "").lower()
    raw  = re.sub(r"\s+", " ", (savings or "").strip())
    s    = raw.rstrip(".")
    coupon_required = bool(re.search(r"with\s+mfr\s+digital\s+coupon", s, re.I))
    s    = re.sub(r"\s*with\s+mfr\s+digital\s+coupon\s*$", "", s, flags=re.I).strip()
    fp   = _num(final_price)
    rp   = _num(regular_price)
    line_save, line_max, line_qty, line_unit, flags = _parse_save_line(save_line)

    o = {"kind": "other", "headline": raw, "context": "", "save_text": "",
         "price": None, "regular": None, "save": None, "save_is_max": False, "pct": None,
         "unit": "ea", "coupon_required": coupon_required or st == "digitalcoupon",
         "extra": "", "flags": flags}

    def set_save(per_unit, is_max, text):
        o["save"], o["save_is_max"], o["save_text"] = (round(per_unit, 2) if per_unit else None), is_max, text

    def line_text(unit=None):
        if line_save is None:
            return ""
        t = f"Save {'up to ' if line_max else ''}{_fmt(line_save)}"
        if (line_unit or unit) == "lb":
            t += "/lb"
        if line_qty and line_qty > 1:
            t += f" on {line_qty}"
        return t

    # 1. TPR: `savings` is the discount; finalPrice is what you pay
    if st == "tpr":
        m = re.match(r"^" + _MONEY + r"$", s)
        disc = _money(m.group(1)) if m else None
        if fp > 0:
            o.update(kind="price", headline=_fmt2(fp), price=fp)
            reg = rp if rp > fp else (fp + disc if disc else None)
            if reg:
                o["regular"] = round(reg, 2)
                o["context"] = f"was {_fmt2(reg)}"
                disc = disc or (reg - fp)
            if disc:
                set_save(disc, False, f"Save {_fmt(disc)}")
        elif disc:
            o.update(kind="amount_off", headline=f"{_fmt(disc)} off")
            set_save(disc, False, f"Save {_fmt(disc)}")
        _finish(o)
        return o

    # 2. Percent off
    m = _RE_PCT.match(s)
    if m:
        o.update(kind="pct_off", headline=f"{int(m.group(1))}% off", pct=int(m.group(1)))
        o["save_text"] = line_text()
        _finish(o, coupon_ctx=True)
        return o

    # 3. Buy N get M free (BOGO family)
    m = _RE_BOGO.match(s)
    if m:
        b, g = _qty(m.group(1)), _qty(m.group(2))
        o.update(kind="bogo", headline=f"Buy {b} Get {g} Free")
        if line_unit == "lb":
            o["unit"] = "lb"
        reg = fp if fp > 0 else (line_save / g if line_save else None)
        if reg:
            each = reg * b / (b + g)
            o["price"], o["regular"] = round(each, 2), round(reg, 2)
            o["context"] = f"≈ {_fmt2(each)}{'/lb' if o['unit'] == 'lb' else ' ea'} when you buy {b + g}"
        if line_save:
            set_save(line_save / (b + g) if line_qty or b + g > 1 else line_save, line_max, line_text())
        _finish(o, coupon_ctx=True)
        return o

    # 4. Buy N get $X off
    m = _RE_BUY_OFF.match(s)
    if m:
        n, off = int(m.group(1)), _money(m.group(2))
        o.update(kind="amount_off", headline=f"Buy {n}, get {_fmt(off)} off")
        set_save(off / n, False, line_text() or f"Save {_fmt(off)} on {n}")
        _finish(o, coupon_ctx=True)
        return o

    # 5. $X off ($Y)   /   Save $X (coupon)
    m = _RE_OFF.match(s) or _RE_SAVE.match(s)
    if m:
        off = _money(m.group(1))
        on  = _money(m.group(2)) if m.re is _RE_OFF and m.group(2) else None
        o.update(kind="coupon" if st == "digitalcoupon" else "amount_off",
                 headline=f"{_fmt(off)} off" + (f" {_fmt(on)}" if on else ""))
        # The headline already says "$2 off"; only show a save line when Publix adds detail
        set_save(off if not on else None, False, line_text())
        _finish(o, coupon_ctx=True)
        return o

    # 6. Multi-buy: 2 for $7.00 / 2/$7.00
    m = _RE_MULTI.match(s)
    if m:
        n, total = int(m.group(1)), _money(m.group(2))
        each = total / n if n else total
        o.update(kind="multi", headline=f"{n} for {_fmt(total)}", price=round(each, 2),
                 context=f"{_fmt2(each)} ea")
        if line_save:
            q = line_qty or n
            set_save(line_save / q, line_max, line_text())
        _finish(o, coupon_ctx=True)
        return o

    # 7. Plain price: $4.99 / $4.99 lb / $5.00 each / Sale Price $X / From $X
    m = _RE_PRICE.match(s)
    if m:
        price = _money(m.group(1))
        unit = "lb" if (m.group(2) or "").lower() == "lb" or line_unit == "lb" else "ea"
        prefix = "From " if s.lower().startswith("from") else ""
        o.update(kind="price", unit=unit, price=price,
                 headline=f"{prefix}{_fmt2(price)}{'/lb' if unit == 'lb' else ''}")
        if line_save:
            set_save(line_save, line_max, line_text(unit))
            if not line_max:
                o["regular"] = round(price + line_save, 2)
                o["context"] = f"was {_fmt2(price + line_save)}{'/lb' if unit == 'lb' else ''}"
        _finish(o, coupon_ctx=True)
        return o

    # 8. Free with purchase (coupon)
    if re.match(r"^free\b", s, re.I):
        o.update(kind="free", headline="Free", context=s[4:].strip() or "with purchase")
        _finish(o, coupon_ctx=True)
        return o

    # 9. Unknown: keep the text as the headline, never invent a price
    o["save_text"] = line_text()
    _finish(o, coupon_ctx=True)
    return o


def _finish(o: dict, coupon_ctx: bool = False):
    """Percent, coupon context, flags in context."""
    if o["pct"] is None and o["save"] and o["price"] and not o["save_is_max"] and o["kind"] == "price":
        reg = o["regular"] or (o["price"] + o["save"])
        if reg > 0:
            o["pct"] = int(round(o["save"] / reg * 100))
    if coupon_ctx and o["coupon_required"] and "coupon" not in o["context"].lower():
        o["context"] = (o["context"] + " · " if o["context"] else "") + "with digital coupon"
    extra_flags = [f for f in o["flags"] if f in ("your choice", "limited time", "low price")]
    if extra_flags and not o["context"]:
        o["context"] = extra_flags[0].capitalize()


def compact_offer(o: dict) -> dict:
    """Drop empty fields (None, "", False, []) to keep API payloads and cache rows small.
    Consumers read every field with .get()."""
    return {k: v for k, v in o.items() if v not in (None, "", False, []) and not (k == "unit" and v == "ea")}


def offer_extra(additional_savings: str) -> str:
    """Stacked deals: 'Save $1.00' on top of the sale -> '+ $1 off with digital coupon'."""
    m = _RE_SAVE.match((additional_savings or "").strip()) or _RE_OFF.match((additional_savings or "").strip())
    if not m:
        return (additional_savings or "").strip()
    return f"+ {_fmt(_money(m.group(1)))} off with digital coupon"


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
    # Priority: "SAVE $X ON N" in save_line > "N for $X" in savings > None
    savings_per_item = parse_per_item_savings(save_line)
    if savings_per_item is None:
        savings_per_item = parse_multi_buy_unit_price(savings)

    # final_price: numeric sale price from the API (0 = no individual price, e.g. BOGO)
    try:
        final_price = float(d.get("final_price") or d.get("finalPrice") or 0)
    except (TypeError, ValueError):
        final_price = 0.0

    # wa_promotion_type_id: 1=Weekly Ad, 4=Extra Savings
    wa_promo_id = d.get("wa_promotion_type_id") or d.get("wa_promotionTypeId") or 1
    try:
        wa_promo_id = int(wa_promo_id)
    except (TypeError, ValueError):
        wa_promo_id = 1

    # v9.3: regular price (TPR `originalDeal`) and stacked extra coupon text
    regular_price      = _num(d.get("regular_price") if d.get("regular_price") is not None else d.get("originalDeal"))
    additional_savings = _clean(d.get("additional_savings") or d.get("additionalSavings") or "")
    offer = classify_offer(saving_type, savings, save_line, final_price, regular_price, additional_savings)
    if additional_savings:
        offer["extra"] = offer_extra(additional_savings)
    # Sort key for "Savings" sort: per-unit savings from the offer when known
    if offer.get("save") is not None:
        savings_per_item = offer["save"]

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
        "regular_price":        regular_price,         # v9.3: TPR regular price; 0 if unknown
        "additional_savings":   additional_savings,    # v9.3: stacked extra coupon text
        "offer":                compact_offer(offer),  # v9.3: normalized offer (see classify_offer)
        "wa_promotion_type_id": wa_promo_id,           # 1=WeeklyAd, 4=ExtraSavings
        "is_bogo":              is_bogo,
        "is_publix_brand":      brand_lower == "publix" or title_lower.startswith("publix "),
        "has_coupon":           bool(d.get("has_coupon") or d.get("hasCoupon")) or saving_type in ("PrintableCoupon", "DigitalCoupon"),
        "is_stacked":           saving_type in ("Stacked", "StackedDeals"),   # v9.2.2: Publix sends "Stacked"
        # v9.3: Publix marks Extra Savings with promotion type 4 under savingType "WeeklyAd"
        "is_extra":             saving_type == "ExtraSavings" or wa_promo_id == 4,
    }
