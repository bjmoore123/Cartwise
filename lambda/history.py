"""lambda/history.py — Deal-history summaries for badges and frequency hints.

v9.2 (Batch 1): replaces the old /deals/history payload, which shipped every
past deal (4.7 MB for 30 weeks at store 1546) and made the browser fuzzy-match
every current deal against ~57,000 past deals (~190 s of CPU after each login).

The server now builds a per-title index once per store (cached in the warm
Lambda) and returns only:
  - badges:  normalized title -> {type, label}, for titles in the newest week
  - freq:    the user's list items -> number of past weeks a matching deal ran
  - points:  per-title chart data, fetched on demand when a deal modal opens

Pure functions only: no AWS calls, so this module is unit-tested directly.
Keep norm_title() in sync with cwHistKey() in frontend/app.js.
"""
import re

# Minimum weeks of history before each signal is shown (mirrors HIST_MIN in app.js)
MIN_WEEKS_REPEAT = 2
MIN_WEEKS_RARE   = 12
RARE_MAX_PAST    = 3      # fewer past weeks than this on sale = "rarely on sale"
FREQ_MATCH_SCORE = 70     # same threshold the old client-side frequency hint used

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_title(title: str) -> str:
    """Normalize a deal title for exact lookup: lowercase, alphanumerics only, single spaces.

    KEEP IN SYNC with cwHistKey() in frontend/app.js.
    """
    return _NON_ALNUM.sub(" ", (title or "").lower()).strip()


def fuzzy_score(item: str, title: str) -> int:
    """Port of fuzzyScore() from app.js (precision only: share of item words found in the title).

    Both arguments are expected lowercase. An item token "matches" when it is a
    substring of some title token, which for space-free tokens is equivalent to
    being a substring of the title itself.
    """
    if not item or not title:
        return 0
    if item in title:
        return 100
    toks = item.split()
    if not toks:
        return 0
    title_toks = title.split()
    matched = sum(1 for t in toks if any(t in tt for tt in title_toks))
    return round(matched / len(toks) * 100)


def build_index(snapshots: list[dict]) -> dict:
    """Build the per-title index from weekly snapshots.

    snapshots: [{"week": "YYYY-MM-DD", "deals": [{"title", "savings"}, ...]}, ...]
               in any order; sorted newest first here.
    Returns {"weeks": [...newest first], "titles": {norm: {week_idx: (title, savings)}},
             "lower_titles": {lower_title: set(week_idx)}}
    """
    snaps = sorted(snapshots, key=lambda s: s.get("week", ""), reverse=True)
    weeks = [s.get("week", "") for s in snaps]
    titles: dict[str, dict[int, tuple]] = {}
    lower_titles: dict[str, set] = {}
    for idx, snap in enumerate(snaps):
        for d in snap.get("deals") or []:
            title = d.get("title") or ""
            key = norm_title(title)
            if not key:
                continue
            titles.setdefault(key, {}).setdefault(idx, (title, d.get("savings", "")))
            lower_titles.setdefault(title.lower(), set()).add(idx)
    return {"weeks": weeks, "titles": titles, "lower_titles": lower_titles}


def compute_badges(index: dict) -> dict:
    """Badge lists for titles in the newest week: {"repeat": [norm...], "rare": [norm...]}.

    Only signals that don't depend on parsing the overloaded `savings` text:
      - repeat: also on sale the previous week
      - rare:   12+ weeks of history and on sale in fewer than 3 of the past weeks
    Price-based badges ("best price") return once deals carry a parsed price (Batch 4).
    Lists of keys (labels added client-side) keep the payload small.
    """
    num_weeks = len(index["weeks"])
    out = {"repeat": [], "rare": []}
    for key, weeks_on in index["titles"].items():
        if 0 not in weeks_on:
            continue
        past = len(weeks_on) - 1
        if num_weeks >= MIN_WEEKS_REPEAT and 1 in weeks_on:
            out["repeat"].append(key)
        elif num_weeks >= MIN_WEEKS_RARE and past < RARE_MAX_PAST:
            out["rare"].append(key)
    out["repeat"].sort()
    out["rare"].sort()
    return out


def item_frequency(index: dict, items: list[str]) -> dict:
    """For each list item, the number of PAST weeks (excluding the newest) with a matching deal.

    Matching is the same fuzzy rule the old client used (score >= 70). Each
    distinct title is scored once per item, not once per weekly appearance.
    """
    out = {}
    lower_titles = index["lower_titles"]
    for raw in items or []:
        name = (raw if isinstance(raw, str) else (raw or {}).get("name", "")) or ""
        item = name.strip().lower()
        if not item or item in out:
            continue
        weeks = set()
        for title, weeks_on in lower_titles.items():
            if fuzzy_score(item, title) >= FREQ_MATCH_SCORE:
                weeks |= weeks_on
        weeks.discard(0)
        out[item] = len(weeks)
    return out


def title_points(index: dict, title: str) -> list[dict]:
    """Chart points for one deal title, oldest first: [{"week", "savings"}]."""
    weeks_on = index["titles"].get(norm_title(title), {})
    weeks = index["weeks"]
    return [
        {"week": weeks[i], "savings": weeks_on[i][1]}
        for i in sorted(weeks_on, reverse=True)
    ]
