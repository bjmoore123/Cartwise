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

try:                                    # Lambda package: shared/ next to this file
    from shared.deal_parser import classify_offer
except ImportError:                     # pragma: no cover
    classify_offer = None

# Minimum weeks of history before each signal is shown (mirrors HIST_MIN in app.js)
MIN_WEEKS_REPEAT = 2
MIN_WEEKS_RARE   = 12
RARE_MAX_PAST    = 3      # fewer past weeks than this on sale = "rarely on sale"
MIN_PRICE_POINTS = 6      # v9.4: past weeks with a known price before "lowest price" shows
SUMMARY_FMT      = 3      # v9.4: index entries carry a price; bump to force a rebuild
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


def deal_price(d: dict):
    """Price per unit you pay, from a history row, or None (v9.4).

    Only real prices count (a sale price or a multi-buy's price each); BOGO
    estimates and "$X off" have no reliable price. Old rows only have
    `savings`, which still yields a price for "$3.99", "$4.99 lb", "2 for $7".
    TPR rows written before v9.4 hold the DISCOUNT in `savings` and no
    final_price, so they give no price rather than a wrong one.
    """
    if classify_offer is None:
        return None
    st = d.get("saving_type") or ""
    fp = d.get("final_price") or 0
    if st.lower() == "tpr" and not fp:
        return None
    o = classify_offer(st, d.get("savings", ""), d.get("save_line", ""), fp, d.get("regular_price") or 0)
    if o.get("kind") in ("price", "multi") and o.get("price"):
        return round(float(o["price"]), 2)
    return None


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
    for idx, snap in enumerate(snaps):
        for d in snap.get("deals") or []:
            title = d.get("title") or ""
            key = norm_title(title)
            if not key:
                continue
            per = titles.setdefault(key, {})
            entry = (title, d.get("savings", ""), deal_price(d))
            # Same title twice in a week (sizes/flavors): keep the lowest known price
            if idx not in per or (entry[2] is not None and (per[idx][2] is None or entry[2] < per[idx][2])):
                per[idx] = entry
    # Derived from `titles` (not the raw rows) so a freshly built index and one
    # restored by deserialize_index() are identical.
    lower_titles: dict[str, set] = {}
    for per in titles.values():
        for idx, (title, _sv, _p) in per.items():
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
    out = {"best": [], "repeat": [], "rare": []}
    for key, weeks_on in index["titles"].items():
        if 0 not in weeks_on:
            continue
        past = len(weeks_on) - 1
        # v9.4 (#9): lowest price seen in the whole history (needs 6+ past prices that vary)
        now_p = weeks_on[0][2] if len(weeks_on[0]) > 2 else None
        past_p = [e[2] for i, e in weeks_on.items() if i != 0 and len(e) > 2 and e[2] is not None]
        if now_p is not None and len(past_p) >= MIN_PRICE_POINTS and now_p <= min(past_p) and now_p < max(past_p):
            out["best"].append(key)
        elif num_weeks >= MIN_WEEKS_REPEAT and 1 in weeks_on:
            out["repeat"].append(key)
        elif num_weeks >= MIN_WEEKS_RARE and past < RARE_MAX_PAST:
            out["rare"].append(key)
    for k in out:
        out[k].sort()
    return out


def _search_struct(index: dict):
    """Lazily build a joined-text search structure over all distinct lowercase titles.

    One str.find() scan over a ~600 KB string per token replaces a Python loop
    over ~16,000 titles per item (the old loop cost ~3-4 s on Lambda for a
    24-item list).
    """
    st = index.get("_search")
    if st is None:
        titles = list(index["lower_titles"].keys())
        starts, pos = [], 0
        for t in titles:
            starts.append(pos)
            pos += len(t) + 1          # "\n" separator; tokens never contain it
        st = {
            "joined": "\n".join(titles),
            "starts": starts,
            "weeks":  [index["lower_titles"][t] for t in titles],
        }
        index["_search"] = st
    return st


def _titles_containing(st: dict, needle: str) -> set:
    """Indices of titles that contain `needle` as a substring."""
    from bisect import bisect_right
    joined, starts = st["joined"], st["starts"]
    out, i = set(), joined.find(needle)
    while i != -1:
        out.add(bisect_right(starts, i) - 1)
        i = joined.find(needle, i + 1)
    return out


def item_frequency(index: dict, items: list[str]) -> dict:
    """For each list item, the number of PAST weeks (excluding the newest) with a matching deal.

    Same rule as fuzzy_score() >= 70 (the old client-side hint): a title matches
    when it contains the whole item, or when enough item tokens are substrings
    of it. Computed with substring search over all titles at once.
    """
    st = _search_struct(index)
    out = {}
    for raw in items or []:
        name = (raw if isinstance(raw, str) else (raw or {}).get("name", "")) or ""
        item = name.strip().lower()
        if not item or item in out:
            continue
        matched = set(_titles_containing(st, item))
        toks = item.split()
        if toks:
            hits = {t: _titles_containing(st, t) for t in set(toks)}
            counts: dict = {}
            for t in toks:                      # duplicates count twice, as in fuzzy_score
                for ti in hits[t]:
                    counts[ti] = counts.get(ti, 0) + 1
            n = len(toks)
            matched |= {ti for ti, m in counts.items() if round(m / n * 100) >= FREQ_MATCH_SCORE}
        weeks = set()
        for ti in matched:
            weeks |= st["weeks"][ti]
        weeks.discard(0)
        out[item] = len(weeks)
    return out


# ── Persistence (v9.2.1) ──────────────────────────────────────────────────────
# The finished index is stored in the history table (sk "_summary" + chunks) so
# a request costs a few small reads instead of re-reading 30+ weeks of rows.

def serialize_index(index: dict) -> bytes:
    """gzip JSON: {weeks, titles: {norm: [base_title, [[week_idx, savings(, title_if_different)], ...]]}}"""
    import gzip, json
    titles = {}
    for k, v in index["titles"].items():
        entries = sorted(v.items())
        base = entries[0][1][0]
        titles[k] = [base, [[i, sv, p] if t == base else [i, sv, p, t] for i, (t, sv, p) in entries]]
    data = {"fmt": SUMMARY_FMT, "weeks": index["weeks"], "titles": titles}
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)


def deserialize_index(blob: bytes) -> dict:
    import gzip, json
    data = json.loads(gzip.decompress(blob).decode("utf-8"))
    fmt = data.get("fmt", 2)
    titles, lower_titles = {}, {}
    for key, (base, entries) in data["titles"].items():
        per = {}
        for e in entries:
            if fmt >= 3:                    # [i, savings, price(, title)]
                t, p = (e[3] if len(e) > 3 else base), e[2]
            else:                           # v9.2.1: [i, savings(, title)]
                t, p = (e[2] if len(e) > 2 else base), None
            per[e[0]] = (t, e[1], p)
            lower_titles.setdefault(t.lower(), set()).add(e[0])
        titles[key] = per
    return {"weeks": data["weeks"], "titles": titles, "lower_titles": lower_titles}


def title_points(index: dict, title: str) -> list[dict]:
    """Chart points for one deal title, oldest first: [{"week", "savings", "price"?}]."""
    weeks_on = index["titles"].get(norm_title(title), {})
    weeks = index["weeks"]
    out = []
    for i in sorted(weeks_on, reverse=True):
        e = weeks_on[i]
        pt = {"week": weeks[i], "savings": e[1]}
        if len(e) > 2 and e[2] is not None:
            pt["price"] = e[2]
        out.append(pt)
    return out
