"""shared/matcher.py — Unified deal matching for scraper + email alerts.

KEEP IN SYNC with frontend/app.js (cwMatchScore / cwMatchDealsForUser) —
the JS port must produce identical results for web UI consistency.

Used by:
  - scraper/main.py  (email alerts)
  - lambda/deals.py  (GET /user/matches — retained for API consumers)

Algorithm:
  1. Normalize both user item text and deal text (strip special chars except
     hyphens, collapse whitespace, lowercase).
  2. Build a composite match string from brand + title + description.
  3. Remove noise words (articles, prepositions, units, quantities).
  4. Score using precision (item words found in composite) and coverage
     (fraction of title words matched) with brand-aware boosting.
  5. Three sensitivity tiers map to fixed internal thresholds.
"""

import re

# ── Sensitivity tiers ─────────────────────────────────────────────────────────

SENSITIVITY_THRESHOLDS = {
    "strict": 80,
    "normal": 65,
    "loose":  50,
}
DEFAULT_SENSITIVITY = "normal"


def _get_threshold(matching_cfg: dict) -> int:
    """Resolve sensitivity config to a numeric threshold.
    Accepts either a raw 'threshold' integer (40-100, from the slider UI)
    or a 'sensitivity' string ('strict'/'normal'/'loose').
    threshold takes precedence if both are present.
    """
    raw = matching_cfg.get("threshold")
    if isinstance(raw, (int, float)) and 40 <= int(raw) <= 100:
        return int(raw)
    sens = matching_cfg.get("sensitivity", DEFAULT_SENSITIVITY)
    return SENSITIVITY_THRESHOLDS.get(sens, SENSITIVITY_THRESHOLDS[DEFAULT_SENSITIVITY])


# ── Text normalization ────────────────────────────────────────────────────────

# Strip everything except alphanumeric, hyphens, and whitespace
_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_COLLAPSE_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalize text: strip special chars (keep hyphens), lowercase, collapse whitespace."""
    if not text:
        return ""
    text = _STRIP_RE.sub("", text)      # remove commas, ®, ™, ', &, etc.
    text = _COLLAPSE_WS.sub(" ", text)   # collapse whitespace
    return text.strip().lower()


def tokenize(text: str) -> list[str]:
    """Normalize then split into tokens."""
    return normalize(text).split()


# ── Noise words (stripped from both sides before scoring) ─────────────────────
# These are filler words that don't help identify a product:
# articles, prepositions, conjunctions, and size/quantity units.

_NOISE = {
    # articles / prepositions / conjunctions
    "a", "an", "the", "and", "or", "of", "for", "with", "in", "on", "to",
    "from", "by", "at", "is", "it", "its", "any", "all", "per", "each",
    # quantity / size markers
    "oz", "fl", "lb", "lbs", "ct", "pk", "pt", "qt", "gal", "ml", "mg",
    "kg", "liter", "liters", "ounce", "ounces", "pound", "pounds",
    "count", "pack", "size",
}

# Purely numeric tokens (e.g. "16", "32", "2") are also noise
def _is_noise(token: str) -> bool:
    return token in _NOISE or token.isdigit()


def _clean_tokens(tokens: list[str]) -> list[str]:
    """Remove noise words and purely numeric tokens."""
    return [t for t in tokens if not _is_noise(t)]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _match_score(item_str: str, deal: dict) -> int:
    """Return 0-100 match score for an item string against a deal.

    Builds a composite from the deal's brand, title, and description,
    then scores the user's item tokens against it.
    """
    if not item_str:
        return 0

    title = deal.get("title", "")
    brand = deal.get("brand", "")
    description = deal.get("description", "")

    if not title:
        return 0

    # Normalize everything
    item_norm = normalize(item_str)
    if not item_norm:
        return 0

    # Build composite: brand + title + description (deduplicated tokens)
    composite_parts = []
    for field in [brand, title, description]:
        composite_parts.extend(tokenize(field))
    # Deduplicate while preserving order
    seen = set()
    composite_tokens = []
    for t in composite_parts:
        if t not in seen:
            seen.add(t)
            composite_tokens.append(t)

    # Also keep a separate title-only token list for coverage scoring
    title_tokens = tokenize(title)

    # Clean noise from item tokens only — we want to find the user's
    # meaningful words in the composite. We do NOT clean the composite
    # because noise words in the deal don't hurt matching; they just
    # shouldn't be required from the user side.
    item_tokens_raw = tokenize(item_str)
    item_tokens = _clean_tokens(item_tokens_raw)

    # If cleaning removed everything (e.g. user typed "16 oz"), fall back to raw
    if not item_tokens:
        item_tokens = item_tokens_raw
    if not item_tokens or not composite_tokens:
        return 0

    # Exact normalized match
    composite_str = " ".join(composite_tokens)
    if item_norm == normalize(title):
        return 100

    # Count how many item tokens appear in the composite
    matched = 0
    for iw in item_tokens:
        if iw in composite_tokens:
            matched += 1
        elif len(iw) >= 5 and any(
            len(ct) >= 5 and (iw.startswith(ct) or ct.startswith(iw))
            for ct in composite_tokens
        ):
            # Prefix match for plurals/possessives (e.g. "chicken" ↔ "chickens")
            matched += 1

    if not matched:
        return 0

    precision = matched / len(item_tokens)

    # Coverage: fraction of TITLE words (not composite) that the item covers.
    # This penalizes long bundle titles like "Any 2 X AND Any 1 Y".
    title_clean = _clean_tokens(title_tokens) or title_tokens
    title_matched = 0
    for tw in title_clean:
        if tw in item_tokens:
            title_matched += 1
        elif len(tw) >= 5 and any(
            len(iw) >= 5 and (tw.startswith(iw) or iw.startswith(tw))
            for iw in item_tokens
        ):
            title_matched += 1
    coverage = title_matched / len(title_clean) if title_clean else 0

    # Single-word items: precision only (user is casting a broad net)
    if len(item_tokens) == 1:
        return 100 if matched else 0

    # Brand boost/penalty using the deal's actual brand field
    brand_norm = normalize(brand)
    if brand_norm:
        # Check if any item token matches the brand
        brand_tokens = brand_norm.split()
        item_has_brand = any(
            iw in brand_tokens or any(
                len(iw) >= 5 and len(bt) >= 5 and (iw.startswith(bt) or bt.startswith(iw))
                for bt in brand_tokens
            )
            for iw in item_tokens
        )
        # Check if user specified a different brand (first non-noise multi-char word)
        item_brand_candidates = [t for t in item_tokens if len(t) >= 4 and t not in brand_tokens]
        if item_brand_candidates and not item_has_brand:
            # User's first significant word doesn't match brand — could be wrong brand.
            # Only penalize if the candidate looks like a brand (not a product word).
            # Check if the candidate appears in title — if not, it's likely a brand mismatch.
            first_candidate = item_brand_candidates[0]
            in_title = first_candidate in title_tokens or any(
                len(first_candidate) >= 5 and len(tt) >= 5
                and (first_candidate.startswith(tt) or tt.startswith(first_candidate))
                for tt in title_tokens
            )
            if not in_title:
                # Brand mismatch — cap score
                return min(45, round((precision * 0.65 + coverage * 0.35) * 100))

        if item_has_brand:
            # Brand match bonus — boost precision slightly
            precision = min(1.0, precision * 1.1)

    # Weighted score: precision-heavy (65/35)
    score = round((precision * 0.65 + coverage * 0.35) * 100)
    return min(100, score)


# ── Public API ────────────────────────────────────────────────────────────────

def match_deals_for_user(
    deals:        list[dict],
    items:        list,
    matching_cfg: dict,
) -> list[dict]:
    """Match deals against a user's item list.

    Args:
        deals: list of parsed deal dicts (from deal_parser.parse_deal)
        items: list of item strings
        matching_cfg: dict with "sensitivity" key ("strict"/"normal"/"loose")

    Returns:
        list of matched deal dicts, sorted by score descending
    """
    threshold = _get_threshold(matching_cfg)

    matches = []
    seen = set()

    for deal in deals:
        title = deal.get("title", "")
        if not title:
            continue

        for item in items:
            # Support both plain strings and legacy {name, mode} dicts
            name = item if isinstance(item, str) else (item.get("name") or "")
            if not name:
                continue

            score = _match_score(name, deal)

            if score >= threshold:
                key = deal.get("id") or title
                if key not in seen:
                    seen.add(key)
                    matches.append({
                        "id":          deal.get("id", ""),
                        "my_item":     name,
                        "deal_name":   deal.get("title", ""),
                        "description": deal.get("description", ""),
                        "brand":       deal.get("brand", ""),
                        "department":  deal.get("department", ""),
                        "savings":     deal.get("savings", ""),
                        "save_line":   deal.get("save_line", ""),
                        "fine_print":  deal.get("fine_print", ""),
                        "valid_from":  deal.get("valid_from", ""),
                        "valid_thru":  deal.get("valid_thru", ""),
                        "image_url":   deal.get("image_url", ""),
                        # v9.3: the email needs these to render the offer and the clip link
                        "saving_type": deal.get("saving_type", ""),
                        "coupon_id":   deal.get("coupon_id", ""),
                        "has_coupon":  deal.get("has_coupon", False),
                        "is_bogo":     deal.get("is_bogo", False),
                        "offer":       deal.get("offer") or {},
                        "match_score": score,
                    })

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches
