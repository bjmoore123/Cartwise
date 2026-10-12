# Publix Deal Checker — Project Summary & Planning Doc

> **Last updated:** 2026-02-27  
> **Status:** Active development — v5 deployed  
> **Stack:** AWS Lambda · DynamoDB · API Gateway · S3 · EventBridge · Resend · Single-page app (vanilla JS)

---

## Overview

A serverless web application that monitors Publix weekly ads, matches deals against each user's personal shopping list, and sends personalized email alerts every Wednesday when items go on sale — including BOGOs and Buy 2 Get 1 deals.

---

## Architecture

```
Browser (SPA)
    │
    ▼
API Gateway (HTTP API)
    │
    ├── API Lambda (api.py)           ← auth, prefs, deals, admin
    │       │
    │       ├── DynamoDB: users
    │       ├── DynamoDB: sessions
    │       ├── DynamoDB: deals-cache
    │       ├── DynamoDB: scrape-logs
    │       ├── DynamoDB: auth-logs
    │       └── DynamoDB: app-logs
    │
    └── Scraper Lambda (main.py)      ← triggered weekly via EventBridge
            │
            ├── Publix Savings API    ← WeeklyAd + Coupons endpoints
            ├── DynamoDB: deals-cache (chunked, 200 deals/chunk)
            ├── DynamoDB: scrape-logs
            └── Resend API            ← email delivery

S3 (static hosting) → index.html
```

---

## Session History

### Session 1 — Initial Build
**Chat:** Grocery deal price tracker  
**Date:** ~Feb 25, 2026

Full-stack build from scratch:
- Serverless AWS architecture (Lambda + DynamoDB + API Gateway + S3 + EventBridge)
- User auth with email + 4-digit PIN, session tokens
- Scraper Lambda fetching Publix savings API weekly
- Fuzzy matching (rapidfuzz) against user shopping lists
- Single-page app frontend with deals browser and admin panel
- Resend integration for weekly email alerts
- `deploy.sh` and `teardown.sh` scripts

---

### Session 2 — Code Review & Bug Triage
**Transcript:** `2026-02-26-14-44-07-grocery-tracker-codebase-review.txt`

- Architecture review, identified security issues and improvement areas
- Flagged admin auth vulnerability (Authorization header intercepted by API Gateway)
- Identified stale match data issue
- Unicode encoding problems noted

---

### Session 3 — v5 Bug Fixes & Feature Enhancements
**Transcript:** `2026-02-26-15-45-31-publix-deal-tracker-v5-fixes.txt`

8 fixes and features:
- **DynamoDB teardown preservation** — `teardown.sh` now preserves tables by default; `--drop-data` flag for full wipe
- **Admin auth fix** — switched from `Authorization` header to `x-admin-secret` (avoids API Gateway interception)
- **Unicode handling** — deal titles/descriptions now decoded from HTML entities
- **Dynamic filters** — Savings filter (Weekly/BOGO/Coupon/Extra) and Department filter populated from live API data
- **Mobile UI** — hamburger nav, responsive layout
- **Cache-based deal refresh** — timestamp comparison avoids redundant API calls
- **Per-user notify email** — alert email address separate from login email
- **Admin panel** — user management table with search, scrape job log viewer, CloudWatch log tail

---

### Session 4 — Deployment Debugging
**Transcript:** `2026-02-26-16-27-11-v5-fixes-filters-unicode-refresh.txt`

- Fixed `resend` import error in Lambda environment
- Fixed Windows path handling in `deploy.sh`
- Simplified filter UI (consolidated to Savings + Department)
- Refined Unicode entity decoding
- Deals cache refresh logic hardened

---

### Session 5 — DynamoDB Chunking & Matches Investigation
**Transcript:** `2026-02-26-18-08-17-v5-chunked-cache-matches-stale.txt`

- **DynamoDB 400KB limit fix** — deals cache now chunked at 200 deals/row with `store_id#0`, `store_id#1`, etc.
- **Email-only-on-schedule flag** — scraper skips email sending on manual/admin-triggered runs
- **Admin pagination & search** — user table now paginated with live search
- Discovered frontend fuzzyScore (5 matches) vs scraper rapidfuzz (11 matches) discrepancy

---

### Session 6 — Fuzzy Matching Investigation
**Transcript:** `2026-02-26-18-40-32-matches-staleness-rapidfuzz-discrepancy.txt`

- Root cause: frontend word-overlap algorithm too conservative vs rapidfuzz
- Frontend `fuzzyScore()` rewritten to prioritise precision (all item words must appear in title)
- Threshold slider added to My Matches for user control

---

### Session 7 — UI Polish & Autocomplete
**Transcript:** `2026-02-27-01-31-57-v5-ui-polish-autocomplete-emails.txt`

- Favicon added (🛒 SVG)
- Auto-save eliminated (explicit Save buttons, cleaner UX)
- PIN masking improved
- Coupon deep-links added to deal cards
- Frontend email redirect for non-`@` inputs
- **Autocomplete for My List** — suggests from current week's deal titles as you type

---

### Session 8 — Admin Analytics & Store Redesign
**Transcript:** `2026-02-27-01-46-05-v5-admin-stats-email-toggle-store-redesign.txt`

- **Admin analytics dashboard** — user count, active stores, email stats, top departments
- **Email opt-out toggle** — per-user email enable/disable with UI toggle
- **Store search redesign** — uses Publix storelocator API directly; real-time search by city, ZIP, or store number; card-based results with address and distance

---

### Session 9 — Store API & BOGO Filter Fixes
**Transcript:** `2026-02-27-01-57-28-store-search-bogo-filter-fixes.txt`

- Fixed store search API headers (`geo+json` Accept header required, `publixstore` header required)
- Fixed BOGO detection — `categories` field from API was the authoritative source, not title scraping

---

### Session 10 — Publix API Investigation (HAR Analysis)
**Transcript:** `2026-02-27-13-26-46-bogo-weekly-ad-api-discovery.txt`

- HAR file analysis of Publix.com network traffic
- Discovered `publixstore` must be sent as a **request header**, not query param
- Discovered WeeklyAd and AllDeals are separate API pools requiring `getSavingType` parameter
- BOGO detection logic updated to check both pools

---

### Session 11 — Admin Sidebar & Logging Expansion
**Transcript:** `2026-02-27-13-39-25-admin-subpages-auth-logging.txt`

Admin panel fully rebuilt:
- **Sidebar navigation** (vertical left nav, mobile-responsive)
- **Users sub-page** — accounts table + authentication log (IP, geo, user agent, pass/fail)
- **Reports sub-page** — analytics dashboard (lazy-loaded)
- **Logging sub-page** — scrape jobs, CloudWatch tail, application logs (frontend JS errors, API errors, email delivery, cache hit/miss), cache stats widget

New infrastructure:
- `auth-logs` DynamoDB table — every login attempt logged with IP/geo/UA
- `app-logs` DynamoDB table — structured logs from frontend, API, email, and cache
- `_log_app_event()` helper in `api.py` — non-blocking log writes
- `_log_email()` helper in `main.py` — logs each email delivery attempt
- Cache hit/miss instrumented in `get_deals()`
- `err()` helper logs 4xx/5xx (excluding 401/404) automatically
- `POST /log/error` public endpoint for frontend JS errors
- `window.onerror` + `window.onunhandledrejection` in frontend

---

### Session 12 (today) — Bug Fixes & UX Improvements
**This conversation**

- **Critical fix:** `_enrich_deal()` in `api.py` was referencing `is_bogo` and `categories` as undefined names → `NameError` crashing every `/deals` call. Fixed by computing both from the deal dict.
- **BOGO filter fix (Matches page):** Default filter was `mf-weekly` (Weekly ad), which excluded BOGOs. Changed to default to "All deals" since Matches is already pre-filtered to the user's list.
- **`isExtra()` fix:** Was checking `'Tpr'` (nonexistent type) instead of `'ExtraSavings'`.
- **Buy 2 Get 1 support:** `isBogo()` regex and `_enrich_deal` backend updated to match `buy \d get \d` pattern — catches B1G1, B2G1, "Buy 2 Get 1 Free", etc.
- **Welcome onboarding modal:** 3-step wizard on first login (What it does → How to set up → Let's go). Per-user `pdc_welcomed_<email>` localStorage flag.
- **First-login routing:** New users land on Store after dismissing welcome modal. Returning users land directly on My Matches.
- **Notifications tab:** Renamed from "Schedule" (📅→🔔). Email Alerts section moved from Account into Notifications tab. Account tab now just PIN and delete.

---

## Current Feature Set

### User-Facing

| Feature | Location | Notes |
|---|---|---|
| Email + 4-digit PIN auth | Auth screen | Register / login / logout |
| Store finder | Store tab | Search by city, ZIP, store# via Publix storelocator API |
| Deal browser | Deals tab | Filter: Savings type + Department; search; add to list from card |
| My Matches | Matches tab | Fuzzy-matched deals from shopping list; threshold slider; all deal types shown by default |
| My List | List tab | Add/remove items; autocomplete from current deals |
| Notifications | Notifications tab | Email on/off toggle; notify-to address; send test email; alert schedule day/time |
| Account | Account tab | Change PIN; delete account |
| Onboarding | First login | 3-step welcome modal → routes to Store |

### Admin (secret-protected)

| Feature | Sub-page | Notes |
|---|---|---|
| User accounts | Users | Table with search/pagination; create user; reset PIN/email/list; delete |
| Authentication log | Users | Every login: IP, geo, UA, success/fail |
| Analytics | Reports | User count, store distribution, email stats, top departments |
| Scrape jobs | Logging | History table + manual trigger |
| CloudWatch tail | Logging | Live Lambda log fetch |
| Application logs | Logging | Frontend JS errors, API errors, email delivery, cache events |
| Cache stats | Logging | Per-store hit/miss rate with bar chart |

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account |
| POST | `/auth/login` | — | Sign in, returns token |
| POST | `/auth/logout` | Bearer | Invalidate session |
| POST | `/auth/change-pin` | Bearer | Update PIN |
| GET | `/user/prefs` | Bearer | Load preferences |
| PUT | `/user/prefs` | Bearer | Save preferences |
| DELETE | `/user/account` | Bearer | Delete account |
| POST | `/user/test-email` | Bearer | Send test alert |
| GET | `/stores/search` | Bearer | Search Publix stores |
| GET | `/deals` | Bearer | Get deals for store (cache-first) |
| GET | `/admin/users` | AdminSecret | List all users |
| POST | `/admin/users` | AdminSecret | Create user |
| GET | `/admin/scrape-logs` | AdminSecret | Scrape job history |
| POST | `/admin/scrape-now` | AdminSecret | Trigger manual scrape |
| GET | `/admin/logs/tail` | AdminSecret | CloudWatch log tail |
| GET | `/admin/stats` | AdminSecret | Analytics dashboard data |
| GET | `/admin/auth-logs` | AdminSecret | Login event log |
| GET | `/admin/app-logs` | AdminSecret | Application log feed |
| POST | `/log/error` | — | Frontend JS error reporting |

---

## DynamoDB Tables

| Table | Key | TTL | Purpose |
|---|---|---|---|
| `*-users` | `email` | — | User accounts + preferences + shopping list |
| `*-sessions` | `token` | `expires_at` | Auth sessions |
| `*-deals` | `store_id` | — | Weekly deals cache (chunked: `store_id#0`, `#1`…) |
| `*-scrape-logs` | `job_id` | — | Scraper run history |
| `*-auth-logs` | `log_id` | — | Login attempts with IP/geo/UA |
| `*-app-logs` | `log_id` | — | Frontend errors, API errors, email delivery, cache events |

---

## Known Bugs Fixed

| Bug | Root Cause | Fix |
|---|---|---|
| Admin auth failing | API Gateway intercepts `Authorization` header | Switched to `x-admin-secret` custom header |
| Stale match data | Frontend cache never invalidated | Timestamp comparison; `loadDeals(false)` on matches panel open |
| DynamoDB 400KB limit | Full deal list in single row | Chunked storage at 200 deals/row |
| `NameError` on `/deals` | `is_bogo`/`categories` bare names in `_enrich_deal` | Compute both from deal dict inside function |
| BOGOs hidden on Matches | Default filter `mf-weekly` excluded BOGOs | Default to `mf-all`; Weekly filter now includes BOGOs |
| `isExtra()` never matched | Checking `'Tpr'` instead of `'ExtraSavings'` | Fixed type string |
| Buy 2 Get 1 not tagged | Regex only matched B1G1/buy one get one | Extended to `buy \d.{0,8}get \d` |
| Frontend/scraper match discrepancy | Different fuzzy algorithms | Frontend `fuzzyScore()` rewritten; threshold slider added |
| Unicode garbage in titles | HTML entities not decoded | `decodeEntities()` applied at render time |
| Resend import error | Missing `resend` package in Lambda layer | Added to requirements, layer rebuild |
| Store search broken | Missing required headers for Publix API | Added `publixstore` header + `geo+json` Accept |

---

## Potential Future Work

### High Priority
- [ ] **Scraper dual-pool fix** — WeeklyAd and AllDeals are separate Publix API pools; scraper should fetch both with correct `getSavingType` parameter to ensure complete deal coverage
- [ ] **Match quality improvement** — rapidfuzz-based matching in the scraper returns ~2x more matches than frontend fuzzyScore; consider exposing pre-computed match results from the scraper directly to the frontend
- [ ] **DynamoDB TTL on deals cache** — old store data never expires; add a TTL of ~10 days
- [ ] **DynamoDB TTL on logs** — auth-logs and app-logs will grow unbounded; add TTL of 30–90 days

### Medium Priority
- [ ] **Push/SMS notifications** — alternative to email (Twilio or similar)
- [ ] **Store validity badge** — show "as of Wednesday" freshness indicator on deals
- [ ] **Deal detail modal** — click a deal card for full info, fine print, coupon barcode link
- [ ] **Price history** — track savings value week-over-week per department
- [ ] **Multi-store support** — users with more than one nearby Publix
- [ ] **Welcome modal: re-trigger** — option to replay onboarding from Account settings

### Low Priority / Nice-to-Have
- [ ] **OAuth login** — Google/Apple sign-in instead of PIN
- [ ] **Admin: edit user prefs inline** — edit store/list/notify email in the admin table row
- [ ] **Export shopping list** — download as plain text or share link
- [ ] **Deal images CDN** — Publix image URLs occasionally 404; consider caching thumbnails
- [ ] **Progressive web app (PWA)** — offline support + home screen install

---

## File Reference

| File | Lines | Role |
|---|---|---|
| `frontend/index.html` | ~2,574 | Complete SPA — auth, all panels, admin, JS logic |
| `lambda/api.py` | ~965 | API Lambda — all routes, auth, deals, admin, logging |
| `scraper/main.py` | ~319 | Scraper Lambda — fetch, cache, match, email |
| `scraper/scraper.py` | — | Publix API fetch logic |
| `scraper/matcher.py` | — | rapidfuzz matching logic |
| `deploy.sh` | — | Full AWS infrastructure deploy + update |
| `teardown.sh` | — | Stack teardown (preserves DynamoDB by default) |

---

## Environment Variables (Lambda)

```
USERS_TABLE, SESSIONS_TABLE, DEALS_TABLE, SCRAPE_LOGS_TABLE
AUTH_LOGS_TABLE, APP_LOGS_TABLE
ADMIN_SECRET
RESEND_API_KEY, RESEND_FROM_NAME, RESEND_FROM_ADDR
FRONTEND_URL
SCRAPER_FUNCTION_NAME
PDC_REGION / AWS_REGION
```
