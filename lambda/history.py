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
MIN_SAVE_POINTS  = 6      # v9.8.2: past weeks with a stated saving before "biggest discount" shows
SUMMARY_FMT      = 6      # v9.8.4: + brands for autocomplete; bump forces a rebuild
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


DEAL_KINDS = ("sale", "bogo", "multi", "coupon", "extra")


def deal_kind(d: dict, o: dict | None = None) -> str:
    """Deal type for one history row. KEEP IN SYNC with deal_tag() (email) and dealTypeKey() (app.js)."""
    st = (d.get("saving_type") or "").lower()
    if o is None:
        o = classify_offer(d.get("saving_type") or "", d.get("savings", ""), d.get("save_line", "")) \
            if classify_offer else {}
    if o.get("kind") == "bogo" or d.get("is_bogo"):
        return "bogo"
    if st == "digitalcoupon":
        return "coupon"
    if st in ("extrasavings", "stacked", "stackeddeals") or d.get("is_extra"):
        return "extra"
    if o.get("kind") == "multi":
        return "multi"
    return "sale"


def deal_saving(d: dict) -> dict:
    """What one week's deal saved, per item (per lb for weighed items) (v9.8.2).

    {"s": dollars or None, "k": deal kind, "m": 1 when Publix said "up to", "u": "lb" when per lb,
     "p": percent when only a % was stated}.
    Uses Publix's own figures only: "Save $1.50", "Save up to $5.49" on a BOGO
    (halved, since that is for the pair), the TPR discount. No estimates; a deal
    with no stated saving gets s=None ("on sale, amount not listed").
    """
    if classify_offer is None:
        return {"s": None, "k": "sale"}
    o = classify_offer(d.get("saving_type") or "", d.get("savings", ""), d.get("save_line", ""),
                       d.get("final_price") or 0, d.get("regular_price") or 0)
    out = {"s": round(float(o["save"]), 2) if o.get("save") else None, "k": deal_kind(d, o)}
    if o.get("save_is_max"):
        out["m"] = 1
    if o.get("unit") == "lb":
        out["u"] = "lb"
    if out["s"] is None and o.get("pct"):
        out["p"] = o["pct"]                     # "40% off": shown in the tooltip, not plotted
    return out


def saved(slot):
    """Dollars saved from an index slot, or None."""
    return slot.get("s") if isinstance(slot, dict) else None


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
    brands: set = set()
    for idx, snap in enumerate(snaps):
        for d in snap.get("deals") or []:
            brand = (d.get("brand") or "").strip()
            if brand:
                brands.add(brand)
            title = d.get("title") or ""
            key = norm_title(title)
            if not key:
                continue
            per = titles.setdefault(key, {})
            entry = (title, d.get("savings", ""), deal_saving(d))
            # Same title twice in a week (sizes/flavors): keep the biggest stated saving
            if idx not in per or _better_slot(entry[2], per[idx][2]):
                per[idx] = entry
    # Derived from `titles` (not the raw rows) so a freshly built index and one
    # restored by deserialize_index() are identical.
    lower_titles: dict[str, set] = {}
    for per in titles.values():
        for idx, (title, _sv, _p) in per.items():
            lower_titles.setdefault(title.lower(), set()).add(idx)
    return {"weeks": weeks, "titles": titles, "lower_titles": lower_titles, "brands": sorted(brands)}


def _better_slot(new, old) -> bool:
    n, o = saved(new), saved(old)
    if n is None:
        return False
    return o is None or n > o


def compute_badges(index: dict) -> dict:
    """Badge lists for titles in the newest week: {"repeat": [norm...], "rare": [norm...]}.

    Only signals that don't depend on parsing the overloaded `savings` text:
      - repeat: also on sale the previous week
      - rare:   12+ weeks of history and on sale in fewer than 3 of the past weeks
      - best:   v9.8.2: this week's stated saving is the biggest in the history
                (6+ past weeks with a saving, and they vary)
    Lists of keys (labels added client-side) keep the payload small.
    """
    num_weeks = len(index["weeks"])
    out = {"best": [], "repeat": [], "rare": []}
    for key, weeks_on in index["titles"].items():
        if 0 not in weeks_on:
            continue
        past = len(weeks_on) - 1
        # v9.8.2: biggest discount seen in the whole history (needs 6+ past savings that vary)
        now_s = saved(weeks_on[0][2]) if len(weeks_on[0]) > 2 else None
        past_s = [saved(e[2]) for i, e in weeks_on.items() if i != 0 and len(e) > 2 and saved(e[2]) is not None]
        if now_s is not None and len(past_s) >= MIN_SAVE_POINTS and now_s >= max(past_s) and now_s > min(past_s):
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
    data = {"fmt": SUMMARY_FMT, "weeks": index["weeks"], "titles": titles, "brands": index.get("brands", [])}
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)


def deserialize_index(blob: bytes) -> dict:
    import gzip, json
    data = json.loads(gzip.decompress(blob).decode("utf-8"))
    fmt = data.get("fmt", 2)
    titles, lower_titles = {}, {}
    for key, (base, entries) in data["titles"].items():
        per = {}
        for e in entries:
            if fmt >= 5:                    # [i, savings, saving-dict(, title)]
                t, p = (e[3] if len(e) > 3 else base), e[2]
            elif fmt >= 3:                  # v9.4 price slot: not a saving, drop it
                t, p = (e[3] if len(e) > 3 else base), None
            else:                           # v9.2.1: [i, savings(, title)]
                t, p = (e[2] if len(e) > 2 else base), None
            per[e[0]] = (t, e[1], p)
            lower_titles.setdefault(t.lower(), set()).add(e[0])
        titles[key] = per
    return {"weeks": data["weeks"], "titles": titles, "lower_titles": lower_titles,
            "brands": data.get("brands", [])}


def _ac_key(term: str) -> str:
    """Same product, different spelling: "Boar's Head® Ham" = "Boar's Head Ham", "Olé" = "Ole".
    KEEP IN SYNC with acKey() in frontend/app.js."""
    import unicodedata
    t = unicodedata.normalize("NFD", term or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", t.lower()).strip()


# Not products: store-wide promos, coupon wording, fine-print fragments, size ranges
_AC_PROMO = re.compile(r"^\$\d|^save \$|^[^a-z0-9]|when you spend|[\r\n]", re.I)
_AC_MAX_LEN = 100


def _ac_clean(term: str) -> str:
    """Strip stray quotes; drop store-wide promos ("$10 off When You Spend $30 on …"),
    which are not products anyone puts on a shopping list."""
    raw = (term or "").replace('"', "").strip()             # '"Dirty" Chips' -> 'Dirty Chips'
    if len(raw) > _AC_MAX_LEN or _AC_PROMO.search(raw):
        return ""
    return " ".join(raw.split())


def autocomplete_terms(index: dict) -> list[str]:
    """v9.8.4: autocomplete list = every deal title and brand this store ran in the
    history window (52 weeks, the history TTL). Replaces the global corpus row, which
    only ever grew and stopped updating at DynamoDB's 400 KB item limit.
    v9.8.5: one spelling per product (ignoring case, accents, ®/™ and punctuation),
    preferring the plainest spelling; stray surrounding quotes removed."""
    best: dict[str, str] = {}
    def offer(raw):
        t = _ac_clean(raw)
        k = _ac_key(t)
        if not k:
            return
        cur = best.get(k)
        rank = lambda x: (sum(1 for c in x if ord(c) > 127), x.isupper(), len(x), x)
        if cur is None or rank(t) < rank(cur):
            best[k] = t
    for per in index["titles"].values():
        for (t, _sv, _p) in per.values():
            offer(t)
    for b in index.get("brands") or []:
        offer(b)
    return sorted(best.values(), key=str.lower)


def title_points(index: dict, title: str) -> list[dict]:
    """Chart points for one deal title, oldest first (v9.8.2):
    [{"week", "savings" (Publix text), "kind", "save"?, "max"?, "unit"?, "pct"?}]."""
    weeks_on = index["titles"].get(norm_title(title), {})
    weeks = index["weeks"]
    out = []
    for i in sorted(weeks_on, reverse=True):
        e = weeks_on[i]
        slot = e[2] if len(e) > 2 and isinstance(e[2], dict) else {}
        pt = {"week": weeks[i], "savings": e[1], "kind": slot.get("k", "sale")}
        if slot.get("s") is not None:
            pt["save"] = slot["s"]
        for src, dst in (("m", "max"), ("u", "unit"), ("p", "pct")):
            if slot.get(src):
                pt[dst] = slot[src]
        out.append(pt)
    return out
