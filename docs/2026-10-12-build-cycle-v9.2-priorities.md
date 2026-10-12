# CartWise — Build Cycle v9.2 Priorities

**Date:** 2026-10-12
**Baseline:** `C:\Cartwise` working tree (last commit `9400bb6`, 2026-03-17, *"updates to design doc, teardown script, and deal sorting for BOGO deals"*). The working tree has uncommitted edits made after that commit (most files were modified 2026-03-20).
**Scope:** A full read of `frontend/`, `lambda/`, `scraper/`, `shared/`, `tests/`, and `deploy.sh`. The 67 existing tests pass. Card pricing logic and the JS/Python matchers were also run against sample deals to confirm the behavior described below.

> **Which repo is current?** `C:\Cartwise` is current: it's under git, has the v9.1 design doc, and was modified about 19 days after `C:\publix_deals`. `C:\publix_deals` is an older copy (no `.git`, no `deals.py`, older `deploy.sh`/`teardown.sh`) and should be archived to avoid confusion. Commit the current working tree before starting this cycle.

---

## 0. Theme of this cycle

**One deal → one offer → one presentation, everywhere.**

Both known issues (*"discounts shown as prices"* and *"BOGO / sale / coupon cards look different"*) come from the same root cause. The Publix `savings` field is overloaded: it can hold a **price** (`"$2.50"`), a **discount** (`"$1.50 off"`, `"Save Up To $2.00"`), a **deal phrase** (`"Buy 1 Get 1 FREE"`, `"Buy 2 Save $3.00"`), or a **multi-buy** (`"2 for $5"`). Every consumer re-guesses what the string means, each in its own way:

| Consumer | Where | How it interprets `savings` |
|---|---|---|
| Deal / match card | `app.js` `savingsDisplay()` L1472 | ~8 regex branches, separate BOGO regex |
| Deal modal | `app.js` L1757 | Raw `savings` in big green "price" type |
| Weekly email | `scraper/main.py` `build_html()` L318 | Raw `savings` under a column headed **"Price"** |
| History badges | `app.js` `computeBadge()` L110 | First `$` amount, **higher = better** |
| History chart | `app.js` `buildDealChart()` L1620 | First `$` amount, **lower = better** |
| Sort "Savings ↓" | `app.js` `dealSortSavings()` L96 | Only the `save` line, everything else sorts last |
| Server sort helper | `deal_parser.py` L103 | `savings_per_item` mixes per-item *savings* and per-unit *price* (and the frontend never reads it) |

Running real-shaped samples through the current card logic:

| Input (`saving_type`, `savings`, `save_line`) | Card shows | Problem |
|---|---|---|
| WeeklyAd, `"$1.50 off"` | **$1.50 off** in green price style | Discount shown as a price |
| WeeklyAd, `"Save Up To $2.00"` | **Save Up To $2.00** in green price style | Discount shown as a price |
| WeeklyAd + `has_coupon`, `"$4.99"`, no save line | **Save $4.99** (amber) | The price became a fake discount |
| WeeklyAd, `"Buy 2 Save $3.00"`, `"SAVE $3.00"` | **$2.00 ea** / Save $1.00 ea | Fake per-unit price: the BOGO math ran on a non-BOGO, and the tag says "Weekly Ad" |
| Tpr, `"$3.99"`, `final_price 3.99` | **$3.99** / Save $3.99 | Price repeated as the savings |
| Tpr, `"$3.99"`, no `final_price` | **Save $3.99** | Price shown as a discount |
| ExtraSavings, `"$1.00 off"` | **Save $1.00 off** | Doubled wording |
| B1G1, `"SAVE UP TO $5.29"` | **$2.65 ea** / Save $2.65 ea | Plausible, but "up to" is presented as exact |

### P0-A. Normalize the offer once, in Python (`shared/deal_parser.py`)

Add a single `classify_offer(deal)` that returns a structured offer stored on every deal:

```text
offer = {
  kind:        "bogo" | "multibuy" | "sale_price" | "amount_off" | "percent_off" | "coupon" | "text",
  price:       float|None   # what you pay for 1 (sale price), never a discount
  unit_price:  float|None   # effective per-unit (multibuy / BOGO)
  qty:         int|None     # multibuy / BOGO bundle size
  save:        float|None   # dollars saved per qualifying purchase
  save_is_max: bool         # "SAVE UP TO" → render "up to"
  headline:    str          # big line on card ("$2.50", "BOGO", "2 for $5", "$1.50 off")
  context:     str|None     # small grey line ("Buy 1 Get 1 Free", "$2.65 ea")
  save_text:   str|None     # red line ("Save up to $5.29")
  coupon:      {id, clip_url}|None
}
```

- One BOGO detector, in Python only. Delete `isBogo()`'s regex fallback and `savingsDisplay()`'s private regex (`app.js` L931, L1479).
- `final_price` wins whenever it is present (> 0) for `sale_price`. A `savings` value equal to `final_price` is a price, not a savings.
- `kind` drives the **one** type tag (BOGO / Sale / Coupon / Extra / Stacked / TPR).
- Persist the `offer` in the deal cache **and** in the history snapshot (`_slim_deal`). History then compares like with like: `price` or `unit_price`, lower = better.
- Frontend `savingsDisplay()` becomes a pure renderer of `offer.headline/context/save_text`. The card, match card, modal, and email all render from the same three fields.
- **Test first:** capture one week of raw Publix JSON for 2–3 stores into `tests/fixtures/` and write a table-driven test over every distinct `savings`/`additionalDealInfo` shape. That fixture table becomes the spec.

---

## 1. P0 — Broken behavior (fix first)

| # | Bug | Where | Effect |
|---|---|---|---|
| 1 | **Login button dies after any validation error.** `_authInFlight = true` is set before the email/PIN checks, which `return` outside the `try/finally`, so the flag is never reset. The PIN auto-submit makes this easy to hit: type the PIN first, the email is blank, and "Sign In" does nothing until the page is reloaded. | `app.js` L385–394 | Users locked out of the form |
| 2 | **Enter key in store search does nothing.** The input has two `data-action` attributes, and browsers keep only the first (`store-input-type`), so `store-search-key` never fires. | `index.html` L653–654 | First-run setup friction |
| 3 | **Autocomplete click is blocked by CSP.** Suggestions use inline `onmousedown="acSelect(...)"`, but CSP is `script-src 'self'` (inline removed in v9.1). Mouse/tap selection silently fails; only the keyboard works. | `app.js` L2857–2859 | List-building broken on mobile |
| 4 | **TPR deals (~63% of all deals) can silently vanish.** `loadTprDeals()` discards the response if `sid !== _dealsStoreId`, but `_dealsStoreId` is set only after the *core* response lands. If TPR returns first (likely on a refresh, since both responses are browser-cached `max-age=300`), the response is dropped on first load. On a same-store reload it merges into the old array, which core then overwrites, while `_tprLoaded = true` blocks any retry. | `app.js` L985, L1006, L1951, L1967 | Matches and browse silently incomplete |
| 5 | **Autocomplete corpus is always empty.** The scraper writes the set to attribute `terms`; the API reads `titles`. | `scraper/main.py` L257 vs `lambda/deals.py` L327 | The "global corpus" feature never worked |
| 6 | **A Publix outage wipes the deal cache.** `get_deals()` swallows both fetch errors and returns `[]`; `cache_deals()` then deletes the old chunks and writes an empty index. Separately, chunks are deleted *before* new ones are written, so readers can see a partial set mid-scrape. | `scraper/scraper.py` L65–79; `scraper/main.py` L102–131, L520–530 | Empty Deals page for a week |
| 7 | **Stacked deals are hidden by default; TPR has no filter.** `isExtra()` checks `'Stacked'` but the value is `'StackedDeals'`. TPR isn't covered by any of the four default checkboxes, so ~63% of deals appear only under "All", while "All" shows the full count. The filter counts don't add up. | `app.js` L937–939, L1290–1297; `index.html` L704–718 | Users never see most deals |
| 8 | **"Savings ↓" sort is mostly a no-op.** It reads only `sv.save`, so coupons, extra, stacked, TPR-without-price, and every "$X off" deal sort as `-Infinity`. BOGO sorts by *per-each* savings while others use the total. | `app.js` L96–103 | Wrong ordering |
| 9 | **History badges and the chart contradict each other.** The badge says *best* when the amount is **highest**; the chart plots **lowest** as best. Both read the overloaded `savings`. `>=` also makes every unchanged repeat deal "🔥 Best price this year". The chart is titled "past 12 months" regardless of how much history exists. | `app.js` L136–155, L1620, L1744 | Misleading quality signals |
| 10 | **Web and email matchers have drifted** (the "KEEP IN SYNC" contract is broken). JS replaces punctuation with a space and its `\w` is ASCII-only; Python deletes punctuation and is Unicode-aware. Measured: `kelloggs frosted flakes` → JS 91 / PY 100; `häagen-dazs` → 83 / 100; `ben & jerry's` → 86 / 82 (so at threshold 85 it matches on web and not in email); JS also gives `ben & jerry's` a 33 against Kellogg's via a stray `s` token. | `matcher.py` L48–58 vs `app.js` L1133–1140 | Email ≠ web matches |
| 11 | **The sensitivity slider shows 75, but matching uses 65** for anyone who hasn't moved it. Default prefs store `{"sensitivity": "normal"}` (= 65); the slider just displays a hard-coded 75. | `helpers.py` L160; `app.js` L588, L1150–1155 | Confusing, inconsistent |
| 12 | **Admin "Reset PIN" doesn't unlock the account.** It doesn't clear `failed_attempts` / `lockout_until`, so a locked-out user still can't sign in for 60 minutes. It also doesn't set `valid_after`. | `admin.py` L195–206 | Support dead-end |
| 13 | **Admin logs aren't the latest.** `scan(Limit=N)` returns an *arbitrary* N items, which is the same gotcha already recorded for app logs. The Auth Log shows random logins; "Last run" can be stale once there are more than 50 scrape rows; inbound logs examine only 2,000 rows of a growing table. | `admin.py` L337, L388, L441 | Wrong operational picture |

---

## 2. P1 — Incongruities (presentation and consistency)

**Cards, modal, email**

- The card, modal, and email each show price differently (see §0). The modal shows raw `savings` at 26px and `save_line` in amber, while the card shows the computed `$X ea`.
- **Email:** the "Price" column header sits over raw `savings` (BOGO text and "$1.50 off" rendered as prices). There are no product images even though `image_url` is in every match. The header says "🛒 Publix Deal Alert" and the subject says "Publix Deals" while the sender is "Cartwise Alerts". There's no deal-type tag and no `List-Unsubscribe` header. Deal and user strings go into the HTML unescaped (`build_html`, `main.py` L296–351).
- **Duplicate signals on one card:** the type tag *and* a badge both say Coupon, Extra, or Stacked (`dealCardHtml` L1818–1823). Match cards omit the Extra/Stacked badges that deal cards show (L1411–1414 vs L1818–1823).
- **Three color systems for the same concept:** Coupon is a *blue* tag, a *purple* badge, and a purple clip link. Extra is a *pink* tag and a *blue* badge. Stacked is a *purple* tag and an *amber* badge. History badges use a third (Bootstrap-like) palette.
- **"✂️ Clip Coupon" on cards is plain text** that opens the modal; only the modal has the actual link. The `?cid=` coupon deep-link format is unverified against publix.com.
- **"Coupon" wins over "Weekly Ad"** when a weekly-ad item carries a coupon (`dealTypeTag` order), and the sale price is dropped.
- **"SAVE UP TO" is rendered as an exact amount** (BOGO `Save $2.65 ea`).

**App copy and labels**

- The welcome tour says "Deal Checker" (the old name) and tells users to "set your notification email", which can't be changed (it's locked to the login email).
- The panel title "Store Settings" doesn't match the nav label "My Store", and "This Week's Deals" doesn't match "Deals".
- "Every Wednesday at 8:00 AM ET": the cron is `0 12 ? * WED *` (UTC), which is 7:00 AM ET from November to March.
- The admin retention note says auth/app logs keep 1 year; the code keeps 90 days (auth) and 30 days (app), and scraper email and cleanup logs have **no TTL at all** (`main.py` L66–77, L431–459).
- `deploy.sh` prints "throttling 20 rps, burst 50" but sets 100/200 (L611–613).
- Registering an *existing* email shows "Account created! Signing you in…" (or "Account created! Please sign in." if the PIN differs).

**UI defects**

- The "Load more" hover state is white text on pale green (`--green-dim`), which is unreadable (`index.html` L478).
- **No filters at all below 900px:** `.filter-col` is `display:none` with no mobile alternative (L456).
- "↻ Refresh" doesn't refresh: `forceRefresh` is unused and responses are browser-cached for 5 minutes.
- The Admin nav item is visible to every user.
- `doLogout()` doesn't clear `_dealHistory`/`_corpusTitles`, so the next user on the same browser briefly sees the previous store's badges.

---

## 3. P2 — Hygiene and tech debt

- `lambda/api.py` (1,667 lines) is **still in the repo**. It isn't packaged by `deploy.sh`, but the notes say it was deleted. Remove it.
- `rapidfuzz` is installed into the scraper zip but never imported (`deploy.sh` L419).
- `tests/test_deal_parser.py` defines `TestBogoDetection`, `TestSavingType`, `TestPublixBrand`, and `TestFieldExtraction` **twice**. The second definitions shadow the first, so the first copies never run.
- Dead code and CSS: `renderTpr`, `#matches-loading-inline`, the `.match-card*` family, `.btn-add-to-list`, `.sensitivity-bar`, and two conflicting `.toggle-switch` blocks (L316–321 vs L552–557).
- `GET /user/matches` and `deals_by_title` enrichment are legacy now that matching is client-side. Keep or delete deliberately. `get_matches` also reports `sensitivity: "normal"` regardless of the slider.
- The verification link doesn't URL-encode `email` (`auth.py` L37), so plus-addressed emails may fail. Verify against API Gateway's `+` handling.
- Inbound: the `added_items` log is wrong (it logs the first N parsed, not the ones actually added). `inbound-subject-rejected` and `inbound-rate-limited` have no admin labels, and an unknown sender isn't logged. **The inbound 403 from Resend `/emails/receiving/{id}` is still unresolved** (carried over from v9.1).
- `delete_account` removes only the current session. `admin_reset_email` orphans sessions keyed to the old email. Cleanup-deleted and self-deleted users aren't counted in "Users Deleted".
- `_week_start()` doesn't parse Publix's `M/D` date format and falls back to "this Monday". This works for the Wednesday cron, but a manual scrape on Mon/Tue labels the old ad as the new week.
- `helpers.UNSUB_SECRET` defaults to `"changeme-set-in-deploy"` while the scraper defaults to `""`. Fail closed in both.
- The design doc v9.1 history key format (`store_id#YYYY-MM-DD`) no longer matches the code (composite PK/SK). Update it in the v9.2 doc.

---

## 4. Suggested sequencing

1. **Housekeeping (½ day):** commit the working tree; archive `C:\publix_deals`; delete `api.py`; fix the duplicate test classes.
2. **Quick P0 fixes (1 day, low risk):** #1 login flag, #2 duplicate `data-action`, #3 autocomplete via delegation, #5 corpus attribute, #7 `StackedDeals` + a TPR filter, #11 slider default, #12 PIN-reset unlock.
3. **Data safety (½ day):** #6 skip caching when the fetch fails or returns 0 deals; write new chunks first, then the index, then delete stale chunks. #4 TPR race (set `_dealsStoreId` before either fetch; merge with a generation counter).
4. **Offer model (core of the cycle, 2–3 days):** fixtures → `classify_offer()` → cache and history carry `offer` → card, modal, and email all render from it → rebuild sort, badges, and chart on `price`/`unit_price` (lower = better).
5. **Matcher parity (½ day):** align `normalize` (delete apostrophes, keep Unicode letters via `/[^\p{L}\p{N}\s-]/gu`), then add a shared JSON fixture of `(item, deal, expected_score)` run by both pytest and a tiny Node test.
6. **Admin correctness (½ day):** #13 paginated scans or a GSI on `ts`; TTLs on all log writers; fix the retention and throttling copy.
7. **UI refresh** (see the companion design suggestions), built on the unified offer fields.

## 5. Verification checklist

- [ ] Fixture table: every distinct `savings`/`save_line` shape → expected `offer` (pytest).
- [ ] The same fixture rendered by card and email shows identical headline, context, and save text.
- [ ] Matcher parity fixture passes in pytest **and** Node.
- [ ] HAR: TPR merged on the first load and on a same-store refresh (deal count = `total_all`).
- [ ] Simulate a Publix fetch failure in the scraper: the previous week's cache is retained.
- [ ] Admin auth log's newest row equals the latest login.
- [ ] Mobile (≤ 600px): filters reachable, autocomplete tap works, Enter in store search works.
