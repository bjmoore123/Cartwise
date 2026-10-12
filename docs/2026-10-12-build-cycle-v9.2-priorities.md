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

Each batch is meant to be built, deployed to **staging**, verified, and then promoted. Batch 1 is the exception: it may go straight to production because it fixes the slowness users are feeling now. Estimates are working days. Progress: **Batches 0–1 done and in production (v9.2.1). Batch 2 and #6 built as v9.2.2, staging next.**

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
- [ ] **Decision needed:** production fonts (DM Serif/Lato) or staging fonts (Fraunces/Outfit). The design canvas assumes production.

### Batch 1: Performance hotfix (P0-B) ✅ code done 2026-10-12, awaiting deploy
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

### Batch 2: Broken interactions ✅ code done 2026-10-12 (v9.2.2), staging next
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
- [ ] #13 admin logs newest-first (GSI on `source`+`ts`, or paginate and sort); stop scanning the whole app-logs table
- [ ] TTL on every log writer (scraper email and cleanup logs have none); stop writing a log row on every cache hit
- [ ] Investigate the 2026-03-30 history snapshot (3,257 deals)
- [ ] Fix the retention, throttling and schedule copy (admin note, `deploy.sh` message, "8 AM ET")

### Batch 4: Offer model and price display
- [ ] Fixtures from one real week of Publix JSON (2–3 stores) → `tests/fixtures/`
- [ ] `classify_offer()` in `shared/deal_parser.py`; the cache and history store `offer`
- [ ] Card, match card, modal and email render `offer.headline / context / save_text`; one BOGO detector
- [ ] #8 sort by `save` / `unit_price`; #9 badges and chart on price
- [ ] Email: drop the "Price" column, add images, type tags and CartWise branding, add a `List-Unsubscribe` header, escape HTML
- [ ] #10 matcher parity: same `normalize` in Python and JS; a shared fixture run by pytest and Node

### Batch 5: UI refresh
- [ ] Reconcile the design canvas with staging's March changes (single tag already done there)
- [ ] One tag palette, price-color rule (green = pay, red = save), history chips in the card footer
- [ ] Mobile filter sheet (no filters below 900px today)
- [ ] Copy fixes: "Deal Checker", "set your notification email", panel titles vs nav labels
- [ ] Link the orphaned icons and `manifest.json` (now in the repo from GitHub)
- [ ] `--ink-soft` to #6b6b6b for contrast; fix the Load-more hover

### Batch 6: Hygiene (ride-along)
- [ ] Delete `lambda/api.py`, the unused `rapidfuzz` install, dead CSS/JS, and duplicate test classes
- [ ] URL-encode the verification email link; inbound log fixes; session cleanup on account delete or email reset
- [ ] Update the design doc (history key format, environments, release process)

## 5. Verification checklist

- [ ] Fixture table: every distinct `savings`/`save_line` shape → expected `offer` (pytest).
- [ ] The same fixture rendered by card and email shows identical headline, context, and save text.
- [ ] Matcher parity fixture passes in pytest **and** Node.
- [ ] HAR: TPR merged on the first load and on a same-store refresh (deal count = `total_all`).
- [x] Simulate a Publix fetch failure in the scraper: the previous week's cache is retained (pytest `TestStoreWorkerSafety`).
- [ ] Admin auth log's newest row equals the latest login.
- [ ] Mobile (≤ 600px): filters reachable, autocomplete tap works, Enter in store search works.
