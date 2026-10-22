# CartWise — Build Cycle v9.2 Priorities

**Date:** 2026-10-12
**Baseline:** the production code, which is identical to what is deployed at cartwise.shopping. The March edits that had never been committed are now committed (`aad5d14`).
**Scope:** A full read of `frontend/`, `lambda/`, `scraper/`, `shared/`, `tests/`, and `deploy.sh`. The 67 existing tests pass. Card pricing logic and the JS/Python matchers were also run against sample deals to confirm the behavior described below. A browser recording (HAR file) of a production login and admin visit was profiled for the performance finding (P0-B).

### Environments and repository (updated 2026-10-12)

| | Folder | Site / API | Git |
|---|---|---|---|
| Production | `C:\Projects\Cartwise\production` | cartwise.shopping / api.cartwise.shopping | Yes. Remote `github.com/bjmoore123/Cartwise` (merged with the Feb–Mar browser-upload history) |
| Staging | `C:\Projects\Cartwise\staging` | staging.cartwise.shopping / api-staging.cartwise.shopping | No (by design) |

- `C:\Cartwise`, `C:\Cartwise_Staging` and `C:\publix_deals` have been retired.
- **Staging is ahead of production on the UI.** It was deployed 2026-03-24 with a partial refresh: Fraunces/Outfit fonts, a gradient top bar, one deal-type tag per card, admin nav hidden behind `#admin`, and a sensitivity default of 80. None of the functional bugs below are fixed on staging.
- **Promotion is "copy staging → production"** (see `docs/promotion-checklist.md`). The source code is environment-neutral: `app.js` keeps the `YOUR_API_GATEWAY_URL` placeholder, and `deploy.sh` (`ENV=staging` or unset) replaces it at upload. *Correction:* an earlier note said a straight copy would point production at the staging API. That was wrong; the staging URL only exists in the deployed file.

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

### P0-B. Performance: history badges freeze the browser (production slowness)

**Symptom:** the site, and especially the admin page, feels very slow after login.

**What the HAR shows** (production, store 1546):

| Request | Time | Payload |
|---|---|---|
| `GET /deals/history` | **4.0 s** | **4.7 MB** (30 weekly snapshots, ~57,000 past deals) |
| `GET /deals?types=core` / `types=tpr` | 1.7 s each | 620 KB / 546 KB |
| `GET /user/prefs` | 1.7 s | small (probably a Lambda cold start) |
| All `/admin/*` calls | 0.11–0.25 s | small, so **not** the bottleneck |
| `GET /deals/corpus` | 5 ms | `{"titles": [], "count": 0}` (confirms bug #5) |

**Root cause:** `computeBadge()` (`app.js` L105) compares every current deal against every past deal with `fuzzyScore()`. Running the real `app.js` code on the data from the HAR takes **~100 ms per deal, ~190 s of CPU** for 1,888 deals. `precomputeBadges()` works in batches of 50, so the page freezes for about 5 seconds at a time, repeatedly, for about 3 minutes after every load. Clicks into Admin wait behind it.
- Worse case: if history arrives before the TPR deals, `loadTprDeals()` computes TPR badges in one go (L1975–1980), a single freeze of about 100 s.
- **It grows every week.** History keeps 52 weeks; it is at 30 today.
- The 2026-03-30 snapshot holds 3,257 deals against ~1,900 normally, which suggests a double scrape that week. Check `history_deals()` week keys.

**Fix: Batch 1 below.**
1. The server builds a per-title summary, at scrape time or in `/deals/history`: `normalized_title → [{week, price, unit_price, save}]` for the last N weeks, plus the precomputed badge. The payload drops from 4.7 MB to tens of KB.
2. The client does a `Map` lookup by normalized title. No `fuzzyScore` loop and no badge work inside `loadTprDeals()`.
3. Badges and the chart use **price** (lower is better), consistent with P0-A. Until the offer model ships, show only "Repeat deal" and "Rarely on sale", which don't depend on parsing `savings`.
4. Raise `cartwise-api` memory from 256 MB to 512–1024 MB. This gives faster cold starts and faster JSON handling of ~600 KB deal payloads, at negligible cost.

Target: history payload under 100 KB, no main-thread task over 50 ms after login, admin usable within 1 s of opening it.

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

## 4. Release batches

Each batch is meant to be built, deployed to **staging**, verified, and then promoted. Batch 1 is the exception: it may go straight to production because it fixes the slowness users are feeling now. Estimates are working days. Progress: **Batches 0–2 and #6 in production (v9.2.2). Batches 3 (most), 4 and 5 built as v9.3 + v9.4, staging next.**

| Batch | Theme | Items | Est. | Risk | Path |
|---|---|---|---|---|---|
| **0** | Environment groundwork | Env config, `.gitattributes`, promotion checklist | ½ | Low | Both |
| **1** | Performance hotfix | P0-B (history summary, Map lookup, Lambda memory) | 1 | Low–Med | Hotfix to prod, then back-port to staging |
| **2** | Broken interactions | #1, #2, #3, #5, #7, #11, #12 | 1 | Low | Staging → prod |
| **3** | Data integrity and ops | #4, #6, #13, log TTLs, double-scrape check | 1 | Med | Staging → prod |
| **4** | Offer model and price display | P0-A, #8, #9, email rework, matcher parity (#10) | 3–4 | Med–High | Staging → prod |
| **5** | UI refresh | Design canvas + staging's March look, reconciled | 2–3 | Med | Staging → prod |
| **6** | Hygiene | Section 3 (P2) list | ½–1 | Low | Rides along with any batch |

### Batch 0: Environment groundwork ✅ done 2026-10-12
- [x] One `deploy.sh` / `teardown.sh` for both environments: staging's `ENV`-aware scripts adopted into the repo (`ENV=staging` → `cartwise-staging-*`, `staging.`/`api-staging.` hosts). The API URL was already injected at upload, so no `config.js` was needed.
- [x] **New finding, fixed:** both environments set a cookie named `session` on the shared `cartwise.shopping` domain, so **logging into staging logged you out of production** (and vice versa). Cookie names and domain now come from Lambda env vars (`SESSION_COOKIE_NAME`, `ADMIN_COOKIE_NAME`, `COOKIE_DOMAIN`); staging uses `session_stg` / `admin_session_stg`. Shared helpers `make_cookie()` / `read_cookie()` live in `helpers.py`.
- [x] **New finding: staging was running on production data until 2026-10-12.** Before the first Batch 0 staging deploy, the staging API accepted production logins, and none of those logins appear in `cartwise-staging-auth-logs` (only 4 entries ever). So the staging Lambdas' table settings had been pointed at the production `cartwise-*` tables by hand. Every pre-Oct-12 staging action (list edits, admin actions, test emails, possibly staging scrapes) touched production data. That may explain the double-size 2026-03-30 history snapshot. The redeploy re-applied the script-defined `cartwise-staging-*` tables, so the environments are now separated. Test accounts must be created on staging.
- [x] `deploy.sh` reuses existing secrets (admin, unsubscribe, webhook) from the live API Lambda when not passed in, and reads the Resend key from SSM. Previously it regenerated them on any deploy that didn't pass them.
- [x] `.gitattributes` (`* text=auto eol=lf`, binary images)
- [x] `docs/promotion-checklist.md`: develop/test on staging, promote, hotfix back-port, rollback
- [ ] SSM secrets stay shared under `/publix/*` (acceptable for now; split only if staging ever needs different keys)
- [x] Fonts: production's DM Serif/Lato (v9.4 default; staging's theme archived in `staging/archive/frontend-2026-03/`)

### Batch 1: Performance hotfix (P0-B) ✅ in production 2026-10-12 (v9.2 + v9.2.1)
- [x] New `lambda/history.py` (pure, unit-tested) builds a per-title index; `/deals/history` returns `{badges: {repeat:[…], rare:[…]}, freq: {item: weeks}}`. The index is cached in the warm Lambda and invalidated by a keys-only signature query when a new weekly snapshot lands.
- [x] `/deals/history?title=…` returns chart points for one deal, fetched only when a deal modal opens.
- [x] Browser: `computeBadge` is a `Map` lookup (`cwHistKey()` ⇄ `norm_title()`, KEEP IN SYNC); `fuzzyScore` removed; frequency hints refresh 1.5 s after list edits.
- [x] Badges limited to "Repeat deal" and "Rarely on sale" until Batch 4 (the "best price" badges read the overloaded `savings` text). Legend updated.
- [x] `cartwise-api` memory 256 → 1024 MB (`API_MEMORY_MB` in `deploy.sh`).
- [x] Measured on the HAR data (30 weeks, store 1546): payload **4.7 MB → 59 KB (18 KB gzipped)**; browser badge work **~190 s → 3 ms**; server index build 0.2 s (cold), frequency for 20 items 0.8 s.
- [x] Tests: 20 new (`tests/test_history.py`), 87 total passing.
- [x] Also shipping with this batch: the #4 TPR race fix (see Batch 3).
- [x] **v9.2 deployed to production 2026-10-12.** Post-deploy HAR: `/deals/history` payload 4.7 MB → 60 KB, `/user/prefs` 1.7 s → 0.4 s, admin unblocked. But the server still took ~4.9 s per history request, because the cache-freshness check re-read every history row (4.7 MB) on each call.
- [x] **v9.2.1 (staging next):** the finished history index is saved in the history table (`sk="_summary"` + gzip chunks of ≤350 KB; ~600 KB in 2 chunks at 30 weeks). Requests do one meta read, plus 2 chunk reads on a freshly started instance. The scraper deletes `_summary` after writing each weekly snapshot, and summaries older than 8 days rebuild anyway. List-item frequency now uses a substring search over all titles at once: identical results on real data, ~19× faster. Expected `/deals/history` time: ~0.3 s from a fresh instance, less when warm. 92 tests passing.
- [x] Tested on staging, approved, deployed to production 2026-10-12.
- Note: with exact title matching, "Rarely on sale" fires on ~30% of this week's deals (566 of 1,854). Watch whether that's too noisy; it can be tightened (e.g. require 20+ weeks of history) without code changes elsewhere.

### Batch 2: Broken interactions ✅ in production 2026-10-12 (v9.2.2)
Test plan: `docs/v9.2.2-test-plan.md`. 18 new pytest tests (110 total) and 12 browser checks with the page's real CSP enforced; 10 of the 12 fail on the current production frontend and all pass on v9.2.2.
- [x] #1 login flag stuck after a validation error: `_authInFlight` is set only after validation passes.
- [x] #2 Enter in store search: one `data-action="store-search"` handles typing and Enter (the browser kept only the first of the two duplicate attributes).
- [x] #3 autocomplete click: inline `onmousedown` replaced by `data-action="ac-select"` and a document `mousedown` listener (with `preventDefault` so the input keeps focus).
- [x] #5 corpus: `/deals/corpus` reads `terms` (what the scraper writes) merged with legacy `titles`. No backfill needed; the data was already under `terms`.
- [x] #7 Publix sends `saving_type: "Stacked"` (12 deals at store 1546), not `StackedDeals`. Parser, card tag and labels now accept both. New **Price drops** filter (`Tpr`, 973 deals) was the missing piece: before, TPR deals only showed under "All deals". Filter counts refresh after the TPR merge, so the TPR count appears and "All" equals the loaded total.
- [x] #11 threshold: the slider shows what the matcher uses (`cwGetThreshold`: stored threshold, else sensitivity tier, default "normal" = 65). Only moving the slider writes a threshold; adding items or changing other settings keeps the stored value (before, any save pinned 75 in production and 80 in staging).
- [x] #12 admin PIN reset clears `failed_attempts`/`lockout_until`/`last_failed_at` and sets `valid_after` (signs out old sessions).

### Batch 3: Data integrity and ops
- [x] #6 scraper safety (v9.2.2): each store fetch retries once (20 s, `FETCH_RETRY_DELAY_S`). If a source still fails or 0 deals come back, the store is skipped: no cache, history or corpus write and no emails, plus an error row in app logs (`source=scraper`, new "Weekly scrape" filter in Admin → Logs). Cache writes are generation-keyed (`store#<gen>#N`): new chunks, then the index row, then delete the old generation, so readers never see a half-written cache. Readers handle legacy `store#N` rows and retry once if a generation swap happens mid-read.
- [x] #4 TPR race: **pulled into the Batch 1 deploy.** Each load gets a generation number, and a TPR response that beats the core response is parked and merged when core lands. Browser-tested in all orderings: old code dropped 973 of 1,888 deals in 4 of 6 cases; new code keeps all 1,888.
- [x] #13 admin logs newest-first: auth logs (v9.3), scrape and inbound logs (v9.5) read fully and sorted; app-logs no longer flooded by cache hits
- [x] TTL on every log writer; daily cache counters instead of a row per hit (v9.3)
- [x] Investigate the 2026-03-30 history snapshot (3,257 deals): audit tool built (v9.6); run it after deploy
- [x] Fix the retention, throttling and schedule copy (v9.6)

### Batch 4: Offer model and price display ✅ built 2026-10-12 as v9.3 (staging next, together with v9.4)
Test plan: `docs/v9.4-test-plan.md`.
- [x] Fixtures from one real week of Publix JSON (store 1546, 1,890 deals): one deal per distinct shape (55) → `tests/fixtures/publix_offers.json`
- [x] **Root cause confirmed from the raw feed:** for price drops (`Tpr`, 1,023 deals) `savings` is the **discount** (`$0.24`); the price you pay is `finalPrice` and the regular price `originalDeal`. The email printed `savings` under "Price". Coupons put "Save $2.00" in the same field.
- [x] `classify_offer()` in `shared/deal_parser.py` → `offer {kind, headline, context, save_text, price, regular, save, pct, unit, coupon_required, extra}`; all 55 real shapes classified, none fall through to "other". Works on old cache rows (TPR regular price = price + discount).
- [x] Card, match card, modal and email all render `offer`; one tag function (`dealTypeKey` ⇄ `email_render.deal_tag`, KEEP IN SYNC)
- [x] Extra Savings is `wa_promotionTypeId == 4` under `savingType: WeeklyAd` (81 deals), not a saving type; `is_extra` fixed. Stacked deals show their extra coupon ("+ $1 off with digital coupon").
- [x] Email (`shared/email_render.py`): no "Price" column; headline/context/save per row, images, type tags and counts, Cartwise header, real validity dates (TPR/coupons carry a "1/1" placeholder), everything HTML-escaped, plain-text part, `List-Unsubscribe` + one-click POST header, clip-coupon link (the matcher never passed `coupon_id`, so the link never showed), capped at 30 rows (Gmail clips ~102 KB).
- [x] #8 sort: Biggest savings (per unit), Biggest % off, Lowest price → v9.4
- [x] #9 history stores a price per week (summary format 3; old rows yield a price for "$3.99", "2 for $7", "$4.99 lb" but not for old TPR rows). "Lowest in N wks" badge (6+ past prices that vary; 112 titles at store 1546); modal chart plots price. → v9.4
- [x] #10 matcher parity: JS `cwNormalize` now deletes punctuation like Python (it inserted a space: "jerry s" vs "jerrys") and is Unicode-aware; Python-style rounding. 339 of 26,880 real pairs differed; now 0. Fixture `tests/fixtures/matcher_parity.json` (3,222 pairs) run by pytest and Node (`tests/matcher_parity.js`). → v9.4

### Batch 3 remainder ✅ built with v9.3
- [x] #13 admin auth logs: read the whole (TTL-bounded) table and sort newest-first; the old `Scan(Limit=100)` returned an arbitrary 100 rows
- [x] TTL on every log writer (scraper email logs: 30 days; cleanup logs: 90 days)
- [x] Stop writing a log row on every `/deals` request: one counter row per day (`cachestats#YYYY-MM-DD`, `hits_<store>` / `misses_<store>`); Admin → Cache sums them
- [x] Investigate the 2026-03-30 history snapshot (3,257 deals): audit tool built (v9.6); run it after deploy
- [x] Fix the retention, throttling and schedule copy (v9.6)

### Batch 5: UI refresh ✅ built 2026-10-12 as v9.4 (production fonts DM Serif/Lato; Ben can still switch)
- [x] **Staging and production frontends are now the same files.** Staging's March theme (Fraunces/Outfit, gradients, `tag-*` classes, default threshold 80) is replaced; the old staging files are kept in `staging/archive/frontend-2026-03/`. Staging's hidden Admin nav + `#admin` route is adopted in both (go to `/#admin`).
- [x] One tag per card over the image: Sale (weekly ad, price drop, $ off) · BOGO · "2 for" · Coupon · Extra, plus "+ Coupon"; source feed moved to the modal's "Deal source" row; duplicate Coupon/Extra/Stacked badges removed; "Publix" is a prefix in the department line
- [x] Price colors: green = what you pay, red = amount off / savings, ink = a mechanic (BOGO, % off); purple "Clip coupon →" is a real link
- [x] Card footer: matched-item chip and one neutral history chip ("Lowest in 30 wks", "Repeat deal", "Rarely on sale")
- [x] Modal: price panel (headline · context | save), Deal source, Matched (with score), history chip, "Price history · last N weeks"
- [x] Mobile: Filters button opens the same filters as a bottom sheet (44 px rows, Reset, "Show N deals"); two-column cards ≤680px
- [x] Copy: "Deal Checker" → Cartwise; badge legend rewritten. Icons and `manifest.json` linked and now uploaded by `deploy.sh`
- [x] `--ink-soft` #8a8a8a → #6b6b6b (4.5:1); Load-more hover no longer turns pale green on white text
- [x] Copy pass: alerts go to the sign-in email; "Store Settings" → "My Store" (v9.7)

### v9.5: Accounts and security ✅ in production 2026-10-12 — test plan `docs/v9.5-test-plan.md`
- [x] Sessions of deleted accounts or changed emails are rejected; account delete, admin delete and admin email change revoke every session for that email
- [x] `UNSUB_SECRET` fails closed (no `changeme` default)
- [x] Verification link URL-encodes the email (plus addresses) and the email HTML is escaped
- [x] "Account created!" no longer shown for an existing email (enumeration-safe server answer unchanged)
- [x] Sign-out clears the previous user's cards, matches and caches from the page
- [x] Admin "Last run" and inbound logs read the full table, newest first (#13 complete)
- [x] "Users Deleted" counts self-deletes and monthly cleanup
- [x] Admin sessions are bound to the current `ADMIN_SECRET`; rotating it ends them
- [ ] Rotate the Resend API key and admin secrets (exposed in chat 2026-10-12): **not doing this; Ben accepted the risk 2026-10-12.** Steps remain in the v9.5 test plan if that changes. Never rotate `UNSUB_SECRET`.

### v9.6: Data accuracy and ops ✅ built 2026-10-12 (with v9.7) — test plan `docs/v9.7-test-plan.md`
- [x] `_week_start()` parses Publix "M/D" dates (majority vote over weekly-ad deals, "1/1" placeholders ignored, year rollover); a Mon/Tue manual scrape no longer files last week's ad as the new week
- [x] `tools/history_audit.py`: per-week counts, outlier and duplicate-id flags, de-duplicate or delete a week with a JSON backup; run against 2026-03-30 after deploy (and any stray 2026-10-12 week from manual runs)
- [x] "↻ Refresh" bypasses the browser cache (`cache: 'reload'`) and recomputes matches
- [x] Schedule shown in the viewer's time zone (cron stays 12:00 UTC: 8 AM EDT / 7 AM EST)
- [x] Admin retention note and `deploy.sh` throttling message match the code

### v9.7: Inbound email import ✅ built 2026-10-12
- [x] 403 root cause: Resend sending-access keys "can only send emails". Import reads with `RESEND_RECEIVING_KEY` (full-access key, SSM `/publix/resend-receiving-key`, optional), falls back to the sending key with an actionable hint
- [x] `added_items` logs the items actually added; unknown senders logged (`inbound-no-user`); admin labels for subject-rejected / rate-limited / rate-limit-error; rejection rows carry `email_id`
- [ ] Create the full-access key and store it in SSM (Ben, optional; not a rotation of existing keys)

### v9.8: BOGO store rule ✅ built 2026-10-12 — test plan `docs/v9.8-test-plan.md`
- [x] Rule by store state (FL: buy two; TN/NC/VA/KY: one at half price; AL/GA/SC: ask, default buy two)
- [x] `bogo_mode` preference (auto/half/two) on My Store, plus a one-time Deals banner where the state is split
- [x] Cards, modal and email price Buy N Get N deals by the rule (`apply_bogo_mode` ⇄ `applyBogoMode`, parity-tested)

### v9.8.1: Fan-out and log noise ✅ in production 2026-10-14
- Scraper fan-out invokes its own function (`AWS_LAMBDA_FUNCTION_NAME`); `deploy.sh` sets `SCRAPER_FUNCTION` on the scraper. Staging had been invoking the production scraper (IAM refused it).
- Frontend error reporter ignores browser-extension scripts, sourceless "Script error." and the ResizeObserver notice; rate-limited.

### v9.8.2: Savings history ✅ built 2026-10-15 — test plan `docs/v9.8.2-test-plan.md`
- The modal chart tracks the **discount per item each week** (Publix's own "Save $X" / "Save up to $X"; BOGO halved), not prices. No estimates: a deal with no stated saving is a hollow dot on the baseline.
- Dots colored by deal type, same as the card tags; lines join only consecutive on-sale weeks; off-sale weeks get a baseline tick. Hover, tap or keyboard focus shows week, amount, type and Publix's text.
- Larger, darker chart text (11.5–14 px).
- Badge: "Lowest in N wks" → "Biggest discount in N wks". Multi-buy tag is teal on cards and in email; amber means BOGO only.
- History summary format 5 (rebuilds on first load per store).

### v9.8.3: Admin user table ✅ built 2026-10-15
- `last_login` saved on each successful sign-in; users without one are backfilled once from the auth log (90-day window), otherwise shown as "—".
- New Last Login column (date, plus "today" / "3 days ago"; full time on hover).
- Every column header except Actions sorts on click; click again to reverse. Default: last login, newest first. Blank values always sort last. Sorting applies to the whole list, before search and paging.

### v9.8.4: Autocomplete from 52-week history; log fixes ✅ built 2026-10-20
- The global corpus row hit DynamoDB's 400 KB item limit; it last updated 2026-04-08, so autocomplete missed six months of products. It only ever grew.
- `GET /deals/corpus?store_id=X` now returns every deal title and brand the store ran in the history window (52 weeks), from the history index. Bounded; per store; gzipped when the browser accepts it; cached per store in the session. The global row is a read-only fallback for a store with no history; the scraper no longer writes it.
- History summary format 6 (adds brands; rebuilds once per store).
- Admin log: "resend requested" rows show as "↻ resend queued", not a failed send; API errors record method and path.

### v9.8.5: Autocomplete relevance ✅ built 2026-10-20
- With ~20,000 terms, "contains ANY typed word, then A–Z" buried the right products ("american chees" listed every "4C … Cheese"). Now: only terms matching the most typed words; scored by whole-word / word-start matches, the typed phrase, the product noun (last word: "Whole Milk" beats "Milk Duds"), this week's sale items (+ "on sale" chip), shorter names first. 12 shown, with "12 of N matches · keep typing to narrow".
- One spelling per product (case, accents, ®/™, punctuation ignored); stray quotes removed; store-wide promos, coupon wording and fine-print fragments dropped (store 1546: 19,907 → 18,095 terms, 847 → 690 KB before gzip).

### v9.8.6: Autocomplete cleanup reaches every browser ✅ built 2026-10-20
- Coupon titles ("Save $1.50 on any ONE …") still showed: this week's deal titles are added in the browser (never server-filtered), and browsers kept the v9.8.4 list (same session key; 6-hour HTTP cache). Now the browser applies the same not-a-product rule (parity-tested), and the cache key and URL are versioned so old lists are dropped.

### v9.9: Feedback and verify-on-Notifications ✅ built 2026-10-22 — test plan `docs/v9.9-test-plan.md`
- 💬 Feedback (sidebar and phone menu): Bug / Idea / Question / Other + message (2,000 chars). Signed-in, verified accounts only; 5/hour and 20/day per account; saved to `cartwise-feedback` (1-year TTL) before emailing.
- Notification email to the SSM `/publix/feedback-to` address (never in code or page); reply-to = the sender's verified account email from the session; message escaped; subject single line. Staging subject says "staging".
- Admin → Feedback: list (new / show done), Reply (mailto), Mark done / Reopen, "new" count badge.
- Notifications page: "Verify your email to get alerts" card with Send verification email / I've verified, check again. Banner kept.
- Fix: reloading the page hid the verify banner (restored sessions assumed verified). `/user/prefs` now returns `email_verified`; the focus/5-minute session check also refreshes it.

### v9.9.1: Deploy packaging fix ✅ built 2026-10-22
- Staging returned 500 on every request after v9.9: `deploy.sh` copied a fixed list of lambda files and `feedback.py` was not on it, so the API crashed on import. Now copies `lambda/*.py`, and deploy runs a smoke test (a preflight request through the real handler) and stops with the error if the API can't start. Tests check packaging and that every local import exists.

### Batch 6: Hygiene (ride-along)
- [x] Delete `lambda/api.py` (stale duplicate parser, never deployed) and the unused `rapidfuzz` install (v9.3)
- [x] Dead CSS/JS removed (49 CSS rules, legacy price guessing, hidden loader, `renderTpr`); duplicate test classes removed so the newer ones run (v9.7)
- [x] URL-encode the verification email link (v9.5); inbound log fixes (v9.7); session cleanup on account delete or email reset (v9.5)
- [x] Design doc updated: `cartwise-design-document-v9.7.md` (history keys, cache generations, offer model, email, environments, release process) (v9.7)
- [x] `GET /user/matches` kept as the server-side email preview; reports the threshold actually used (v9.7)

## 5. Verification checklist

- [x] Fixture table: every distinct `savings`/`save_line` shape → expected `offer` (pytest `test_offer.py`).
- [x] Card, modal and email all render the same `offer` fields (v9.3; `test_offer.py`, browser checks).
- [x] Matcher parity fixture passes in pytest **and** Node.
- [x] TPR merged on first load and refresh in every response order (race harness, 1,888/1,888).
- [x] Simulate a Publix fetch failure in the scraper: the previous week's cache is retained (pytest `TestStoreWorkerSafety`).
- [x] Admin auth log's newest row equals the latest login (v9.3).
- [x] Mobile (≤ 600px): filters reachable (v9.4 sheet), autocomplete tap works, Enter in store search works (v9.2.2).
