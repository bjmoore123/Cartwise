# CartWise — Technical Design Document

> **Version:** 9.7 (October 2026). Supersedes v9.1; see §0 for what changed.
> **Status:** Production
> **Stack:** AWS Serverless (Lambda, DynamoDB, API Gateway, S3, CloudFront, EventBridge)
> **Domain:** cartwise.shopping
> **Codebase:** ~9,200 lines across 17 source files

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Infrastructure Architecture](#2-infrastructure-architecture)
3. [Data Model (DynamoDB)](#3-data-model-dynamodb)
4. [API Surface](#4-api-surface)
5. [Deal Scraping Pipeline](#5-deal-scraping-pipeline)
6. [Matching Engine](#6-matching-engine)
7. [Email Notifications](#7-email-notifications)
8. [Inbound Email Import](#8-inbound-email-import)
9. [Frontend Application](#9-frontend-application)
10. [Authentication & Security](#10-authentication--security)
11. [Admin System](#11-admin-system)
12. [Deployment & Operations](#12-deployment--operations)
13. [Constraints & Limits Reference](#13-constraints--limits-reference)
14. [Security Changelog (v9.0 → v9.1)](#14-security-changelog-v90--v91)
15. [Environments & Release Process](#15-environments--release-process)

---

## 0. What changed since v9.1 (October 2026)

| Release | Highlights |
|---|---|
| v9.2 / v9.2.1 | History summary computed on the server and persisted (`/deals/history` 4.7 MB → 60 KB, browser CPU ~190 s → 3 ms); per-environment cookie names; staging separated from production data; secrets reused on deploy; TPR load race fixed |
| v9.2.2 | Login recovers after validation errors; Enter in store search; CSP-safe autocomplete; corpus reads `terms`; "Stacked" saving type; Price drops filter; threshold slider shows the effective value; admin PIN reset clears lockout; scraper keeps the last good cache (retry, skip on failure, generation-keyed chunks) |
| v9.3 | **Offer model**: `classify_offer()` normalizes every deal (TPR `savings` is the discount; the price is `finalPrice`); cards, modal and email render the same offer; email rebuilt (`shared/email_render.py`); Extra Savings = promotion type 4; ops log fixes; `lambda/api.py` removed |
| v9.4 | Sorts by savings / % off / price; history stores price per week, "Lowest in N wks" chip, price chart; JS and Python matchers give identical scores (parity fixture); UI refresh (one tag per card, price-color rule, mobile filter sheet); staging and production frontends unified; admin nav behind `/#admin` |
| v9.5 | Sessions of deleted accounts / changed emails rejected and revoked; open tabs return to sign-in when the session ends; admin sessions bound to `ADMIN_SECRET`; `UNSUB_SECRET` fails closed; verification link encoding; enumeration-safe registration copy |
| v9.6 | Ad week parsed from Publix "M/D" dates; Refresh bypasses the browser cache; schedule shown in the viewer's time zone; history audit tool (`tools/history_audit.py`); copy fixes |
| v9.8 | BOGO store rule: half price on one (TN/NC/VA/KY) vs buy two (FL); AL/GA/SC ask the user; `bogo_mode` pref; cards, modal and email price Buy N Get N deals accordingly |
| v9.7 | Inbound import reads received email with a separate full-access key (`RESEND_RECEIVING_KEY`); inbound logging fixes; dead CSS/JS and duplicate tests removed; this document |

## 1. System Overview

CartWise is a serverless grocery savings application that monitors Publix weekly ads and digital coupon pools, matches available deals against user-curated shopping lists, and delivers personalized email alerts when relevant items go on sale. The system serves as both a deal browsing platform and an intelligent matching engine that connects user preferences with active promotions.

The application operates on a fully serverless AWS architecture, requiring zero always-on compute. Costs scale linearly with usage and remain near-zero at low user counts.

### 1.1 Core Capabilities

**Deal Aggregation:** Fetches and merges Weekly Ad deals and Digital Coupon pools from the Publix REST API, deduplicating across both sources and enriching each deal with computed boolean fields (BOGO, coupon, Publix brand, extra savings, stacked deals, TPR).

**Fuzzy Matching:** A unified matching engine (`shared/matcher.py`) scores user shopping list items against deal composites (brand + title + description) using precision/coverage weighting with brand-aware boosting, noise word filtering, and a user-adjustable match threshold slider (40–100).

**Email Alerts:** Weekly EventBridge-triggered scraper runs send personalized HTML deal alert emails via Resend, with HMAC-based one-click unsubscribe links.

**Email Import:** Users forward their Publix "My List" email to a Resend inbound address; a webhook parses the HTML table and merges items into their shopping list automatically.

**Deal History:** Weekly snapshots are written to a deal-history table (52-week TTL) with a global deal-corpus for persistent autocomplete suggestions. The server builds a per-store index from the snapshots (persisted under `sk="_summary"`) and returns small summaries: history chips ("Lowest in N wks", "Repeat deal", "Rarely on sale"), list-item frequency, and per-title price points for the modal chart.

**Admin Dashboard:** Full user management, scraper controls, log viewers (auth, app, inbound, CloudWatch tail), and platform statistics.

### 1.2 High-Level Data Flow

1. EventBridge fires a cron rule (Wednesdays 12:00 UTC) invoking the Scraper Lambda.
2. The scraper groups registered users by store, fetches deals once per unique store from the Publix API, and caches results in DynamoDB.
3. For each user, the shared matcher scores their shopping list items against cached deals using their chosen match threshold.
4. If matches are found and the user has email enabled and their email is verified, an HTML alert email is sent via Resend.
5. History snapshots and corpus updates are written for long-term analytics and autocomplete.
6. When users visit the web UI, they can browse all deals, view matches (computed in the browser by a JS port of the shared matcher; a parity fixture keeps both identical), manage their shopping list, and adjust their match threshold.

---

## 2. Infrastructure Architecture

All infrastructure is provisioned by a single idempotent `deploy.sh` script (~1,310 lines). The system uses no Terraform, CloudFormation, or CDK — direct AWS CLI calls wrapped in shell functions handle creation, updates, and idempotency checks.

### 2.1 Architecture Diagram

```
Browser (SPA)
    │  HTTPS (CloudFront)
    ▼
CloudFront Distribution
    ├── / → S3 (frontend/index.html + frontend/app.js)
    └── /api/* → API Gateway (api.cartwise.shopping)
                   │
    ┌──────────────┴──────────────┐
    │                             │
    ▼                             ▼
API Lambda (cartwise-api)    Scraper Lambda (cartwise-scraper)
    │                             │
    ├── DynamoDB: users           ├── Publix Savings API
    ├── DynamoDB: sessions        ├── DynamoDB: deals (write)
    ├── DynamoDB: admin-sessions  ├── DynamoDB: deal-history (write)
    ├── DynamoDB: deals (read)    ├── DynamoDB: deal-corpus (write)
    ├── DynamoDB: deal-history    ├── DynamoDB: scrape-logs (write)
    ├── DynamoDB: deal-corpus     ├── DynamoDB: app-logs (write)
    ├── DynamoDB: scrape-logs     └── Resend API (email send)
    ├── DynamoDB: auth-logs
    ├── DynamoDB: app-logs
    ├── Resend API (verify/test)
    └── Resend Inbound Webhook
                                  EventBridge (weekly cron)
                                       │
                                       └── → Scraper Lambda
```

### 2.2 AWS Services

| Service | Role |
|---|---|
| Lambda | API handler + Scraper (Python 3.12, 256MB, 30s/120s timeout) |
| DynamoDB | All data storage (on-demand billing, 9 tables) |
| API Gateway | HTTP API with `$default` catch-all route |
| S3 | Static frontend hosting (OAC-locked) |
| CloudFront | HTTPS termination, geo headers, response header policy |
| EventBridge | Weekly scraper cron (Wednesdays 12:00 UTC) |
| SSM Parameter Store | Stable secret storage (RESEND_API_KEY, UNSUB_SECRET) |
| ACM | TLS certificates for cartwise.shopping + api.cartwise.shopping |

### 2.3 Lambda Architecture

Both Lambdas share a `shared/` directory containing the deal parser and matching engine. This ensures deals are parsed and matched identically whether the user is receiving an email alert or browsing matches in the web UI.

**API Lambda (cartwise-api):** Modules: `handler.py` (router), `helpers.py` (utilities/constants), `auth.py`, `prefs.py`, `deals.py`, `admin.py`, `inbound.py`, `logging_utils.py`, `shared/deal_parser.py`, `shared/matcher.py`. The handler is a thin routing layer — a path/method lookup table that delegates to the appropriate module function. No business logic lives in the router.

**Scraper Lambda (cartwise-scraper):** Modules: `main.py` (entry point + email logic), `scraper.py` (Publix API client), `shared/deal_parser.py`, `shared/matcher.py`. Invocation modes: (1) EventBridge scheduled → scrape + email all users; (2) Admin manual trigger → scrape + cache only; (3) Single-user resend → scrape + email one user.

### 2.4 Network Architecture

The frontend is served via CloudFront (HTTPS) with the S3 bucket locked down via Origin Access Control. The API Gateway is exposed on a custom domain (`api.cartwise.shopping`) via a regional ACM certificate. API calls are routed through CloudFront via a `/api/*` cache behavior that forwards geo headers to the API Gateway origin.

CORS is configured at both the API Gateway level (preflight) and Lambda level (response headers). Allowed origins are dynamically derived from `FRONTEND_URL`, automatically including both apex and www subdomain variants. The `Vary: Origin` header is always set to prevent cache poisoning.

### 2.5 Security Headers

CloudFront response headers policy and frontend meta tags apply the following:

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains; preload` |
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self' https://cartwise.shopping https://*.cartwise.shopping; img-src 'self' data: https:; frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |

The CSP `script-src` directive is `'self'` only — no `unsafe-inline`. All event handlers use delegated `addEventListener` calls via `data-action` attributes.

---

## 3. Data Model (DynamoDB)

All tables use `PAY_PER_REQUEST` billing (on-demand). Tables with TTL use the `expires_at` attribute for automatic expiration cleanup by DynamoDB.

### 3.1 cartwise-users

**Primary key:** `email` (S). Stores user accounts, credentials, and all preferences in a single item.

| Field | Type | Description |
|---|---|---|
| `email` | S | Login email (partition key) |
| `pin_hash` | S | scrypt hash: `scrypt$<hex_salt>$<hex_hash>` |
| `created_at` | S | ISO 8601 timestamp |
| `prefs` | M | Preferences object (see 3.1.1) |
| `email_verified` | BOOL | Whether email verification link has been clicked |
| `verify_token` | S | Pending verification token (removed after verification) |
| `verify_token_exp` | S | Token expiry (ISO 8601, 48 hours from creation) |
| `failed_attempts` | N | Consecutive failed login count (removed on success) |
| `lockout_until` | S | Lockout expiry (ISO 8601, removed on success) |
| `valid_after` | N | Unix timestamp — sessions created before this are rejected |

#### 3.1.1 Preferences Object (`prefs`)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `store_id` | S | 3-5 digit Publix store number | Selected store |
| `store_name` | S | | Full store name from Publix API |
| `store_address` | S | | Store street address |
| `notify_email` | S | Must match login email | Email for alerts |
| `email_enabled` | BOOL | | Whether weekly alerts are on |
| `items` | L | Max 200 items, 200 chars each | Shopping list |
| `matching` | M | `{threshold: 40-100}` (default 75) | Match threshold from slider |
| `notifications` | M | `{only_matches: bool}` | Alert preferences |

Allowed keys are enforced by `ALLOWED_PREF_KEYS`: `store_id`, `store_name`, `store_address`, `notify_email`, `email_enabled`, `items`, `matching`, `notifications`. Any other keys in a PUT request are silently dropped.

### 3.2 cartwise-sessions

**Primary key:** `token` (S). **TTL:** `expires_at` (72 hours from creation).

| Field | Type | Description |
|---|---|---|
| `token` | S | 64-character hex token (partition key) |
| `email` | S | User email |
| `expires_at` | N | Unix timestamp |
| `session_created_at` | N | Unix timestamp (compared against `valid_after`) |

### 3.3 cartwise-admin-sessions

**Primary key:** `token` (S). **TTL:** `expires_at` (12 hours). Identical structure to user sessions but with shorter lifetime. No email field.

### 3.4 cartwise-deals

**Primary key:** `store_id` (S). **TTL:** 10 days. Chunked storage pattern: an index row at `store_id` (metadata: `count`, `num_chunks`, `fetched_at`, `gen`) plus chunk rows at `store_id#<gen>#0`, `store_id#<gen>#1`, … (legacy rows: `store_id#N`). Each chunk holds up to 200 parsed deals as a JSON string. Writes create the new generation's chunks, then switch the index, then delete the old generation, so readers never see a partial set.

### 3.5 cartwise-deal-history

**Keys:** `store_id` (PK, S) + `sk` (sort key, S). **TTL:** 52 weeks. Index row `sk="YYYY-MM-DD"` (Monday of the ad week) plus chunk rows `sk="YYYY-MM-DD#N"`. Slim deal records: `id`, `title`, `savings`, `saving_type`, `is_bogo`, `department`, `brand`, and since v9.4 `final_price`, `save_line`, `regular_price`. The persisted history index lives at `sk="_summary"` (meta, `fmt=3`) + `sk="_summary#N"` (gzip chunks ≤350 KB); the scraper deletes it after each weekly write and it is rebuilt on demand.

### 3.6 cartwise-deal-corpus

**Primary key:** `corpus_id` (S). Single row with `corpus_id="global"` containing a DynamoDB string set of all deal titles ever seen. Powers persistent autocomplete suggestions that survive weekly cache rotations.

### 3.7 Log Tables

| Table | PK | TTL | Purpose |
|---|---|---|---|
| `cartwise-auth-logs` | `log_id` (S) | 90 days | Login attempts: timestamp, email, success/failure, IP, user agent, reason, CloudFront geo (country, city) |
| `cartwise-app-logs` | `log_id` (S) | 30 days (cleanup logs 90) | Structured events: source (frontend/api/email/cache/scraper/inbound-rate/rate-limit), level, event fields. Also rate-limit counters and daily cache counters (`cachestats#YYYY-MM-DD`, `hits_<store>`/`misses_<store>`). |
| `cartwise-scrape-logs` | `job_id` (S) | 1 year | Scraper run summaries: stores examined, total deals, emails sent, errors, per-store details, timing |

---

## 4. API Surface

All routes are served by the API Lambda via a `$default` catch-all API Gateway route. The `handler.py` router maps method + path to handler functions. All responses include dynamic CORS headers. Authentication is via HttpOnly session cookie (primary) or `Authorization: Bearer` header (fallback).

**API Gateway throttling:** 20 requests/second sustained, 50 burst.

### 4.1 Authentication Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create account (rate-limited: 5/IP/hour) |
| POST | `/auth/login` | — | Sign in, sets HttpOnly session cookie |
| POST | `/auth/logout` | Session | Invalidate current + all other sessions |
| POST | `/auth/change-pin` | Session | Update PIN (invalidates all sessions) |
| GET | `/auth/verify-email` | — | Email verification link handler (returns HTML) |
| POST | `/auth/resend-verification` | — | Resend verification email (rate-limited: 3/email/hour) |

### 4.2 User Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/user/prefs` | Session | Load preferences |
| PUT | `/user/prefs` | Session | Save preferences |
| DELETE | `/user/account` | Session | Delete account and session |
| GET/POST | `/user/unsubscribe` | Token | HMAC-verified unsubscribe (no session needed) |
| POST | `/user/test-email` | Session | Send test alert (rate-limited: 6/user/hour) |
| POST | `/user/resend-weekly` | Session | Re-trigger weekly email (rate-limited: 3/user/hour) |
| GET | `/user/matches` | Session | Server-side matches ("what would this week's email contain"); the web page matches in the browser |

### 4.3 Deal Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/stores/search?q=...` | Session | Search Publix stores by city, ZIP, or store number |
| GET | `/deals?store_id=...&types=...` | Session | Get deals for store (cache-first, live fallback) |
| GET | `/deals/history?store_id=...[&title=...]` | Session | History summary (badges, list-item frequency), or price points for one title |
| GET | `/deals/corpus` | Session | Global deal title set for autocomplete (24h cache) |

### 4.4 Admin Routes

All admin routes require a valid admin session (HttpOnly `admin_session` cookie or `Authorization: AdminToken <token>` header). The `@require_admin` decorator gates every endpoint.

| Method | Path | Description |
|---|---|---|
| POST | `/admin/login` | Exchange raw secret for HttpOnly cookie session (12h) |
| POST | `/admin/logout` | Delete admin session, clear cookie |
| GET | `/admin/users` | List all users with prefs summary |
| POST | `/admin/users` | Create user (admin bypasses rate limit) |
| GET | `/admin/users/{email}` | Full user record (pin_hash redacted) |
| DELETE | `/admin/users/{email}` | Delete user |
| POST | `/admin/users/{email}/reset-pin` | Reset user PIN |
| POST | `/admin/users/{email}/reset-email` | Change user email (duplicate check) |
| PUT | `/admin/users/{email}/prefs` | Patch user prefs (ALLOWED_PREF_KEYS enforced) |
| DELETE | `/admin/users/{email}/items` | Clear user shopping list |
| GET | `/admin/stats` | Platform analytics |
| GET | `/admin/scrape-logs` | Scraper run history |
| POST | `/admin/scrape-now` | Trigger manual scrape (cache only, no emails) |
| GET | `/admin/logs/tail` | CloudWatch log tail (last 150 lines) |
| GET | `/admin/auth-logs` | Authentication event log |
| GET | `/admin/app-logs` | Application event log (filterable by source) |
| GET | `/admin/inbound-logs` | Inbound email events (filterable by sender) |

### 4.5 Other Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/log/error` | Session | Frontend JS error reporting |
| POST | `/inbound/email-list` | Svix HMAC | Resend inbound webhook |

---

## 5. Deal Scraping Pipeline

### 5.1 Data Sources

**WeeklyAd:** `services.publix.com/api/v4/savings` with `getSavingType=WeeklyAd`. Returns BOGO deals, regular sales, extra savings, and TPR (temporary price reductions).

**AllDeals:** Same endpoint with `getSavingType=AllDeals`. Returns digital coupons and printable coupons.

Both requests require a `publixstore` header (not a query parameter). Responses return HTML-encoded strings inside JSON; the deal parser runs `html.unescape()` at ingestion time so entities never reach storage or API responses.

### 5.2 Deduplication

Deals are deduplicated by their `id` field. The WeeklyAd pool is processed first; any deals from the AllDeals pool that share an existing id are skipped.

### 5.3 Caching Strategy

Each store fetch retries once (20 s). If a source fails or 0 deals come back, the store is skipped: no cache, history or corpus write and no emails, plus an `app_logs` error (`source=scraper`). Otherwise deals are cached with generation-keyed chunks (§3.4). Every deal is parsed by `shared/deal_parser.parse_deal()`, which adds the normalized `offer` (§5.5). Cache TTL is 10 days.

### 5.4 History & Corpus

**Deal History:** A slim snapshot of each deal is written to `deal-history` under `sk="YYYY-MM-DD"`. The week is the Monday of the most common real `valid_from` among weekly-ad deals (Publix sends "M/D"; TPR and coupons carry a "1/1" placeholder). 52-week TTL. `tools/history_audit.py` lists weeks, flags outliers and can de-duplicate or delete a week (with a JSON backup).

**Deal Corpus:** All deal titles and brands are merged into the DynamoDB string set `terms` at `corpus_id="global"` (the API also reads the legacy `titles`). This set grows over time and powers autocomplete suggestions that persist even after weekly cache rotation.

### 5.5 Offer Model (v9.3)

`classify_offer()` turns Publix's overloaded `savings` text into one structure: `kind` (price, multi, bogo, amount_off, pct_off, coupon, free), `headline` (what you pay or the mechanic), `context`, `save_text`, `price` (per unit you pay), `regular`, `save`, `pct`, `unit`, `coupon_required`, `extra`. Key facts from the raw feed: for `Tpr` deals `savings` is the **discount** and `finalPrice` the price; Extra Savings is `wa_promotionTypeId=4`; Stacked deals carry `additionalSavings`. `tests/fixtures/publix_offers.json` holds one real deal per distinct shape (55).

---

## 6. Matching Engine

Located in `shared/matcher.py`, this is the single source of truth for all deal matching. It is used by both the scraper (pre-computed matches for email alerts) and the API Lambda (on-the-fly matches for the web UI). A JavaScript port exists in `frontend/app.js` for client-side matching; both carry `KEEP IN SYNC` comments.

### 6.1 Text Normalization

All text is normalized before comparison: strip special characters (® ™ , ' & " ( )), preserve hyphens ("deli-sliced" stays as one token), collapse whitespace, lowercase.

### 6.2 Composite Match String

The engine builds a composite from three deal fields: brand + title + description. Tokens are deduplicated while preserving order. A user who types "Boar's Head turkey" matches even if the title is just "Deli Turkey" and the brand field contains "Boar's Head".

### 6.3 Noise Word Filtering

Filler words are removed from user input tokens: articles (a, an, the), prepositions (of, for, with), conjunctions (and, or), measurement units (oz, lb, ct, pk, gal), and purely numeric tokens.

### 6.4 Scoring Algorithm

For each user item × deal combination: exact match = 100; otherwise count matched tokens (including prefix matching for plurals, 4-char minimum). Final score = `(precision × 0.65 + coverage × 0.35) × 100`. Brand match gives a 10% precision boost; brand contradiction caps score at 45. Single-word items score 100 if found, 0 otherwise.

### 6.5 Match Threshold

The frontend exposes a slider (range input, 40–100) that sets a numeric `threshold` on the user's `matching` config. The default is 75. A deal must score at or above the threshold to appear in the user's matches.

| Threshold | Behavior |
|---|---|
| 90–100 | Very strict — only near-exact matches |
| 75 | Default — balanced precision and recall |
| 60 | Broad — catches partial and multi-word matches |
| 40 | Very loose — includes low-confidence matches |

The backend matcher (`shared/matcher.py`) also accepts a legacy `sensitivity` string (`strict`/`normal`/`loose`) which maps to fixed thresholds (80/65/50) as a fallback for API consumers that don't send a numeric value. The slider UI always sends `{threshold: N}`.

### 6.6 Deduplication

Each deal can only appear once in results (deduplicated by deal id). Results are sorted by score descending.

---

## 7. Email Notifications

### 7.1 Weekly Alert Emails

When the scraper finds matches for a user with `email_verified=True` and `email_enabled=True`, it sends an email via Resend rendered by `shared/email_render.py`: the same headline/context/save as the web cards, one type tag per row, product images, clip-coupon links, real validity dates, HTML-escaped content, a plain-text part, and `List-Unsubscribe` + `List-Unsubscribe-Post` headers (one-click). Capped at 30 rows. Sender address: `alerts@cartwise.shopping`.

### 7.2 Unsubscribe Flow

Each email's unsubscribe link contains the user's email and an HMAC-SHA256 token derived from `email + UNSUB_SECRET`. GET shows a confirmation page with a POST button (prevents email client link prefetch from unsubscribing). POST sets `email_enabled=false`. No session required — token is the only credential.

### 7.3 Email Verification

New accounts start with `email_verified=False`. A verification email is sent at registration containing a `secrets.token_urlsafe(32)` token that expires in 48 hours. Until verified, no alert emails are sent and test email is blocked.

---

## 8. Inbound Email Import

Users forward their Publix "My List" email to `cartwise@minaushii.resend.app`.

### 8.1 Spam Defenses (in order)

1. **Svix HMAC signature** — proves Resend sent the webhook. Timestamp validated within ±5 minutes to prevent replay attacks.
2. **To-address check** — must be addressed to the configured `INBOUND_EMAIL_ADDR`.
3. **Subject allowlist** — subject must contain one of: "my list", "publix", "shopping list", "grocery list".
4. **Per-sender rate limit** — max 5 emails per sender per hour (DynamoDB atomic counter with hourly reset and 2-hour TTL).
5. **User lookup** — drops emails from unregistered senders (login email; `notify_email` is locked to it) and logs `inbound-no-user`.

### 8.2 Processing Pipeline

1. Webhook received → signature verified → subject checked → rate limit checked → user matched.
2. Full HTML body fetched from Resend (`GET /emails/receiving/{email_id}`). Webhooks carry only metadata. Resend "sending access" keys can only send, so this call uses `RESEND_RECEIVING_KEY` (a full-access key from SSM `/publix/resend-receiving-key`), falling back to the sending key with a 403 hint in the logs.
3. Custom `HTMLParser` extracts item names from the Publix My List table (looks for `Name`/`Quantity` headers, takes column 1 from data rows).
4. Parsed items merged into user's `prefs.items`, skipping case-insensitive duplicates.

### 8.3 Logging

Every step writes a structured `app_log` event with the `inbound-` action prefix. The admin panel's Inbound Logs viewer filters by this prefix and supports sender-based filtering.

---

## 9. Frontend Application

The frontend is a single-page application built with vanilla HTML, CSS, and JavaScript — no build step, no framework. `index.html` (~1,217 lines of markup + styles) and `app.js` (~2,981 lines of application logic).

### 9.1 Panels

| Panel | Description |
|---|---|
| Deals | Browse all deals with savings-type (Weekly ad, Price drops, BOGO, Coupons, Extra) and department filters, search, and sorts (biggest savings, % off, lowest price, name). Matches first. Paginated (50/page). Mobile: filters in a bottom sheet. |
| My List | Add/remove items with autocomplete from deals + corpus. Bulk import. Inbound email import instructions. Export to text/clipboard. |
| Notifications | Email on/off toggle, test email, resend weekly, schedule preferences. |
| My Store | Store finder by city/ZIP/store number. Store card with hours, address, phone. |
| Account | Change PIN, view welcome tour, delete account. |
| Admin | Full dashboard (see section 11). |

### 9.2 Event Handling

All user interactions use delegated event listeners bound in `DOMContentLoaded`. Elements declare their intent via `data-action` attributes (e.g., `data-action="add-item"`, `data-action="nav-panel" data-panel="deals"`). A central dispatcher in `app.js` routes click, change, input, keydown, and blur events to the appropriate handler functions. No inline `onclick`/`onchange` handlers exist anywhere in the codebase.

Image error fallback uses a `data-img-fallback` attribute with a capture-phase error listener.

### 9.3 Authentication UI

Login and registration use a 4-digit PIN input with custom auto-advance behavior. Session persistence uses an HttpOnly cookie (primary) with a `localStorage` email cache for session restoration on page load. PIN change shows a confirmation message then signs the user out after 1.5 seconds (all sessions are invalidated server-side on PIN change).

### 9.4 Mobile Responsiveness

Sidebar navigation on desktop, slide-out hamburger menu on mobile (≤680px). Filters open as a bottom sheet below 900px. Deal cards use two columns on phones.

### 9.5 Rendering rules (v9.4)

One type tag per card over the image (Sale, BOGO, "N for", Coupon, Extra) plus "+ Coupon"; headline green for what you pay, red for an amount off, ink for a mechanic; save line red; history chips neutral grey in the card footer. Tag logic: `dealTypeKey()` in `app.js` ⇄ `deal_tag()` in `shared/email_render.py` (KEEP IN SYNC). Any 401 from a user endpoint returns the tab to sign-in; the session is re-checked on focus and every 5 minutes.

---

## 10. Authentication & Security

### 10.1 PIN Hashing

User PINs are hashed with scrypt (`n=16384, r=8, p=1, dklen=32`) using a 16-byte random salt. Stored format: `scrypt$<hex_salt>$<hex_hash>`. Verification uses `hmac.compare_digest` for timing-safe comparison. Legacy unsalted SHA-256 hashes are rejected (require re-registration).

### 10.2 PIN Denylist

The following 20 common PINs are rejected at registration, PIN change, and admin PIN reset:

```
1234, 0000, 1111, 2222, 3333, 4444, 5555, 6666,
7777, 8888, 9999, 1212, 6969, 1122, 1004, 2000,
2580, 0852, 1010, 0123
```

Error message: "That PIN is too common. Please choose a less obvious PIN."

### 10.3 Session Management

Sessions are 64-character hex tokens with a 72-hour TTL. The primary transport is an `HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping` cookie. The `Authorization: Bearer` header is accepted as fallback. On logout, the current session is deleted and a `valid_after` timestamp is set on the user record, which invalidates all other active sessions for that user. PIN change also sets `valid_after`, forcing re-authentication on all devices.

### 10.4 Brute-Force Protection

After 5 consecutive failed login attempts, the account is locked for 60 minutes. The lockout timestamp is stored on the user record. On successful login, the counter is cleared. The error message for failed logins is always "Invalid email or PIN." regardless of whether the account exists or how many attempts remain — this prevents email enumeration and attempt counting side-channels. Timing-safe PIN verification runs even for nonexistent accounts.

### 10.5 Anti-Enumeration

Registration returns a generic message ("If this email is not already registered, a verification email has been sent.") regardless of whether the email already exists. Login returns "Invalid email or PIN." for all failure cases. The `/auth/resend-verification` endpoint returns a generic success message for nonexistent accounts.

### 10.6 Admin Authentication

Admin access uses session-based authentication only. The raw `ADMIN_SECRET` is exchanged (via `POST /admin/login`) for a short-lived 12-hour session token. The token is set as an `HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping` cookie (`admin_session`). The `@require_admin` decorator gates all admin endpoints. Raw secret fallback has been removed — CLI and deploy.sh must call `/admin/login` first.

### 10.7 Rate Limiting

All rate limits use DynamoDB atomic counters with hourly reset and 2-hour TTL. Counters are stored in `app_logs` table with key format `rate#<scope>#<key>#<YYYYMMDD-HH>`.

| Scope | Key | Limit | Endpoint |
|---|---|---|---|
| `register` | Client IP | 5/hour | `POST /auth/register` |
| `resend-verify` | Email address | 3/hour | `POST /auth/resend-verification` |
| `test-email` | User email | 6/hour | `POST /user/test-email` |
| `resend-weekly` | User email | 3/hour | `POST /user/resend-weekly` |
| `inbound` | Sender email | 5/hour | `POST /inbound/email-list` |

Additionally, API Gateway enforces a global throttle of 20 requests/second sustained with a 50-request burst limit.

### 10.8 Input Validation

| Input | Validation |
|---|---|
| Email | ASCII-only, RFC 5321 printable characters regex. Local part: `a-zA-Z0-9!#$%&'*+/=?^_`{\|}~.-`. Domain: alphanumeric + hyphen + dot, TLD ≥ 2 chars. |
| PIN | Exactly 4 digits (`^\d{4}$`). Must not be in PIN denylist. |
| `notify_email` | Must match login email (prevents email relay abuse). |
| Preference keys | Filtered to `ALLOWED_PREF_KEYS` allowlist on both user and admin save. |
| Item strings | Capped at 200 characters each. |
| Item list | Capped at 200 items. |
| Matching threshold | Integer 40–100 (from slider). Legacy sensitivity strings (`strict`/`normal`/`loose`) accepted as fallback. |
| Frontend log messages | `message` capped at 500 chars, `stack` at 1000, `url` at 200. Level must be `error`, `warn`, or `info`. |

### 10.9 CORS Policy

Allowed origins are derived from `FRONTEND_URL`, including both apex (`cartwise.shopping`) and www subdomain. Requests from other origins receive no `Access-Control-Allow-Origin` header. `Access-Control-Allow-Credentials: true` is set for credentialed requests. `Vary: Origin` is always present.

### 10.10 Cookie Configuration

| Cookie | Attributes | TTL |
|---|---|---|
| `session` (user) | `HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping; Path=/` | 72 hours |
| `admin_session` | `HttpOnly; Secure; SameSite=Lax; Domain=cartwise.shopping; Path=/` | 12 hours |

### 10.11 IP Handling

Client IP is extracted only from API Gateway's authoritative `requestContext.http.sourceIp`. The `X-Forwarded-For` header is explicitly not used because it is attacker-controllable. Geo-IP data (country, city) is populated from `CloudFront-Viewer-Country` and `CloudFront-Viewer-City` edge headers when available.

### 10.12 Webhook Security

Resend inbound webhooks are verified using Svix HMAC-SHA256 signature verification. The `svix-timestamp` is validated within ±5 minutes of server time to prevent replay attacks. The webhook secret (`whsec_...`) is base64-decoded and used to HMAC the signed content (`svix_id.svix_timestamp.raw_body`).

### 10.13 Content Security Policy

`script-src 'self'` — no inline scripts permitted. All frontend interaction uses `data-action` attribute delegation with `addEventListener`. `connect-src` is restricted to `'self' https://cartwise.shopping https://*.cartwise.shopping`. `frame-ancestors 'none'` prevents clickjacking. `style-src` allows `'unsafe-inline'` for CSS (required by the inline `<style>` block).

---

## 11. Admin System

The admin panel is a dashboard embedded in the SPA, activated by entering the `ADMIN_SECRET` in a login form. The session token is stored as an HttpOnly cookie (not in JavaScript-accessible storage).

### 11.1 User Management

View all users with store, item count, and match threshold. Per-user actions: view full record, reset PIN (denylist enforced), change email (duplicate check), patch preferences (ALLOWED_PREF_KEYS filtered), clear shopping list, delete account. Deletion increments a `__meta__` record's `deleted_count`.

### 11.2 Scraper Controls

View last 15 scraper run summaries. Trigger manual scrape (cache-only, no emails). View last 150 lines from scraper's CloudWatch log stream.

### 11.3 Log Viewers

Authentication Logs (login attempts with IP, user agent, geo, success/failure), Application Logs (all structured events, filterable by source), Inbound Logs (email import pipeline, filterable by sender), CloudWatch Tail (raw scraper Lambda output).

### 11.4 Platform Statistics

Total users, deleted users, average items per user, users with no items, registrations by month, users by geography (from store name), top 25 shopping list items.

---

## 12. Deployment & Operations

### 12.1 Deploy Script (`deploy.sh`)

A ~1,310-line idempotent Bash script that provisions and updates the entire AWS stack. Safe to re-run — existing resources are skipped or updated in place.

**Usage:**

```bash
# Basic deploy (no custom domain — S3 HTTP frontend):
RESEND_API_KEY="re_xxx" ADMIN_SECRET="yourpassword" bash deploy.sh

# Full deploy with CloudFront + custom domain:
ACM_CERT_ARN="arn:aws:acm:us-east-1:ACCOUNT:certificate/ID" \
APP_DOMAIN="cartwise.shopping" \
RESEND_API_KEY="re_xxx" ADMIN_SECRET="yourpassword" bash deploy.sh
```

**Environment Variables:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `RESEND_API_KEY` | Yes | — | Resend API key for email delivery |
| `ADMIN_SECRET` | No | Auto-generated | Secret for admin panel login. Auto-generated on first deploy if not set. |
| `UNSUB_SECRET` | No | Auto-generated | HMAC salt for unsubscribe tokens. Rotating this invalidates all existing unsubscribe links. |
| `RESEND_WEBHOOK_SECRET` | No | `not-yet-configured` | Svix signing secret from Resend Webhooks dashboard. Inbound email rejects all requests until set. |
| `INBOUND_EMAIL_ADDR` | No | `cartwise@minaushii.resend.app` | Resend inbound address for list-import emails |
| `ACM_CERT_ARN` | No | — | ARN of an ISSUED ACM certificate in us-east-1. Required for CloudFront/custom domain (steps 9–15). |
| `APP_DOMAIN` | No | — | Apex domain (e.g., `cartwise.shopping`). Required for CloudFront/custom domain (steps 9–15). |
| `RESEND_FROM_NAME` | No | `Cartwise Alerts` | Display name on outbound emails |
| `RESEND_FROM_ADDR` | No | `onboarding@resend.dev` | From address for outbound emails |

**Deployment Steps (15 total):**

| Step | Resource | Notes |
|---|---|---|
| 1 | DynamoDB tables (9) | Created if missing; TTL enabled on sessions, admin-sessions, deals, auth-logs, app-logs, deal-history |
| 2 | IAM role | `cartwise-lambda-role` with DynamoDB, Lambda invoke, CloudWatch, SSM, ACM permissions |
| 3 | API Lambda | `cartwise-api` — packages `lambda/` + `shared/`, applies env vars |
| 4 | Scraper Lambda | `cartwise-scraper` — packages `scraper/` + `shared/`, 120s timeout |
| 5 | SSM secrets | Stores `RESEND_API_KEY`, `RESEND_FROM_NAME`, `RESEND_FROM_ADDR` in Parameter Store |
| 6 | API Gateway | HTTP API with `$default` route → API Lambda. Throttling set to 20 rps / 50 burst. |
| 7 | Frontend S3 | Uploads `index.html` + `app.js` to S3 bucket |
| 8 | EventBridge | Weekly scraper cron (`cron(0 12 ? * WED *)`) + monthly cleanup (`cron(0 13 1 * ? *)`) |
| 9* | S3 log bucket | `cartwise-cf-logs` for CloudFront access logs |
| 10* | CloudFront OAC | Origin Access Control for S3 |
| 11* | Response headers policy | HSTS, X-Frame-Options, X-Content-Type-Options |
| 12* | CloudFront frontend dist | S3 origin with OAC, ACM cert, apex + www aliases |
| 13* | Route 53 + API CloudFront | DNS records, API CloudFront distribution with geo-header origin request policy |
| 14* | Cache invalidation | `/*` invalidation on frontend CloudFront |
| 15* | S3 website config | Error document configuration |

*Steps 9–15 only run when both `ACM_CERT_ARN` and `APP_DOMAIN` are set.*

**Environment variable delivery:** Lambda env vars are applied via a Python-generated JSON file (`apply_env` helper) to avoid shell interpretation of special characters in secrets like `ADMIN_SECRET`.

**SSM Parameter Store:** `RESEND_API_KEY` and `UNSUB_SECRET` are stored in SSM on first deploy and retrieved on subsequent deploys, ensuring stable values across re-deployments. This prevents unsubscribe link invalidation and API key rotation on every deploy.

### 12.2 Teardown Script (`teardown.sh`)

A 577-line script that removes all AWS resources created by `deploy.sh`. DynamoDB tables (user data) are preserved by default.

**Usage:**

```bash
# Preview what would be deleted (no changes made):
bash teardown.sh --dry-run

# Delete all infra, keep DynamoDB data:
bash teardown.sh

# Delete everything including all user data:
bash teardown.sh --delete-data

# Combine flags:
bash teardown.sh --delete-data --dry-run
```

**Command-Line Options:**

| Flag | Description |
|---|---|
| *(none)* | Delete all AWS resources except DynamoDB tables. Prompts for confirmation. |
| `--delete-data` | Also delete all 9 DynamoDB tables (irreversible — all user accounts, deals, logs are lost). |
| `--dry-run` | Preview mode — lists every resource that would be deleted without making any changes. No confirmation prompt. |

**Teardown Steps (15 total, dependency-ordered):**

| Step | Resource | Notes |
|---|---|---|
| 1 | CloudFront frontend distribution | Disables first (async ~5-10 min wait), then deletes |
| 2 | CloudFront API distribution | Same disable-then-delete pattern |
| 3 | CloudFront OAC, response headers policy, origin request policy | All three CloudFront policies |
| 4 | Route 53 DNS records | A records for apex, www, and api subdomain. ACM validation CNAME preserved. |
| 5 | API Gateway custom domain | Deletes API mapping first, then domain name |
| 6 | API Gateway HTTP API | |
| 7 | EventBridge rules | Both `cartwise-weekly` and `cartwise-monthly-cleanup`, with auto-target removal |
| 8 | Lambda functions | `cartwise-api` and `cartwise-scraper` |
| 9 | IAM role | Detaches all managed policies, deletes all inline policies, then deletes role |
| 10 | DynamoDB tables (9) | Only with `--delete-data`; otherwise preserved with `✦` marker |
| 11 | SSM parameters | `/publix/resend-api-key`, `/publix/resend-from-name`, `/publix/resend-from-addr` |
| 12 | CloudWatch log groups | `/aws/lambda/cartwise-api` and `/aws/lambda/cartwise-scraper` |
| 13 | CloudWatch alarms + SNS topics | Checks both legacy `pdc-*` and current `cartwise-*` naming |
| 14 | S3 buckets | `cartwise-cf-logs` (log bucket) and `cartwise-frontend-{account}` (frontend) |
| 15 | Temp file cleanup | Removes `/tmp/cf_config_disabled.json` etc. |

**Output:** Each resource prints one of four status markers:

| Marker | Meaning |
|---|---|
| `✓ Deleted` | Resource found and successfully removed |
| `- Not found` | Resource doesn't exist (already deleted or never created) |
| `✦ Preserved` | Resource intentionally kept (DynamoDB without `--delete-data`) |
| `⚠️ Failed` | Deletion attempted but failed (re-run to retry) |

A summary line shows totals for each category. CloudFront distribution failures are expected on the first run if the distribution hasn't finished disabling — re-running resolves this.

**Resources NOT deleted (require manual action):**

| Resource | Reason | Manual command |
|---|---|---|
| ACM certificate | May be shared with other services | `aws acm delete-certificate --certificate-arn <ARN>` |
| Route 53 hosted zone | Contains ACM validation CNAME needed for cert renewal | Delete via AWS console |
| Route 53 domain registration | Billing/ownership concern | Manage via AWS console |

### 12.3 Environment Variables (Lambda Runtime)

Both Lambda functions receive these environment variables at runtime:

```
USERS_TABLE, SESSIONS_TABLE, DEALS_TABLE, SCRAPE_LOGS_TABLE
AUTH_LOGS_TABLE, APP_LOGS_TABLE, ADMIN_SESSIONS_TABLE
HISTORY_TABLE, CORPUS_TABLE
ADMIN_SECRET, UNSUB_SECRET, RESEND_WEBHOOK_SECRET
RESEND_API_KEY, RESEND_FROM_NAME, RESEND_FROM_ADDR
INBOUND_EMAIL_ADDR
FRONTEND_URL, API_URL
SCRAPER_FUNCTION
PDC_REGION
```

### 12.4 Operational Monitoring

Three monitoring layers: DynamoDB `scrape-logs` (per-run job summaries), structured `app-logs` (all application events with 30-day TTL), and CloudWatch Logs (raw Lambda output). The admin dashboard surfaces all three with filtering and real-time tail capabilities.

### 12.5 Cost Structure

With serverless pricing, the system has near-zero baseline cost: Lambda charges only for invocations (weekly scraper runs + API requests), DynamoDB charges per read/write with on-demand billing, S3 storage is minimal (two frontend files), and CloudFront charges per request with a generous free tier. At low user counts, monthly cost is typically under $1.

### 12.6 File Reference

| File | Lines | Role |
|---|---|---|
| `frontend/index.html` | 1,217 | SPA markup + styles |
| `frontend/app.js` | 2,981 | Application logic + event delegation |
| `lambda/handler.py` | 93 | Thin routing layer |
| `lambda/helpers.py` | 337 | Shared utilities, constants, auth, rate limiter |
| `lambda/auth.py` | 363 | Registration, login, logout, PIN change, email verification |
| `lambda/prefs.py` | 296 | Preferences, unsubscribe, test email, resend weekly |
| `lambda/deals.py` | 416 | Store search, deals, history, corpus, matches |
| `lambda/admin.py` | 408 | Admin endpoints, user management, scraper controls |
| `lambda/inbound.py` | 338 | Inbound email webhook, spam filters, HTML parser |
| `lambda/logging_utils.py` | 99 | Auth and app log writers, frontend error endpoint |
| `shared/deal_parser.py` | 116 | Unified deal enrichment/normalization |
| `shared/matcher.py` | 274 | Matching engine (Python — source of truth) |
| `scraper/main.py` | 604 | Scraper entry point, email composition, history/corpus |
| `scraper/scraper.py` | 79 | Publix API fetch logic |
| `deploy.sh` | 1,311 | Full AWS infrastructure provisioning |
| `teardown.sh` | 577 | Stack teardown with --dry-run and --delete-data |

---

## 13. Constraints & Limits Reference

### 13.1 User Constraints

| Constraint | Value |
|---|---|
| PIN format | Exactly 4 digits (0-9) |
| Denied PINs | 20 common PINs (see §10.2) |
| Email format | ASCII-only, RFC 5321 printable characters |
| Notify email | Must match login email |
| Shopping list max items | 200 |
| Item name max length | 200 characters |
| Match threshold range | 40–100 (slider), default 75. Legacy: `strict` (80), `normal` (65), `loose` (50). |

### 13.2 Rate Limits

| Action | Limit | Key | Lockout |
|---|---|---|---|
| Login attempts | 5 consecutive failures | Per account | 60-minute lockout |
| Registration | 5 per hour | Per IP address | 429 response |
| Resend verification | 3 per hour | Per email address | Silent drop (generic success) |
| Test email | 6 per hour | Per user email | 429 response |
| Resend weekly email | 3 per hour | Per user email | 429 response |
| Inbound email import | 5 per hour | Per sender email | Silent drop |
| API Gateway global | 20 rps / 50 burst | All requests | 429 response |

### 13.3 Timeouts & TTLs

| Item | Duration |
|---|---|
| User session | 72 hours |
| Admin session | 12 hours |
| Email verification token | 48 hours |
| Deal cache | 10 days |
| Deal history snapshots | 52 weeks |
| Auth logs | 90 days |
| App logs | 30 days |
| Rate limit counters | 2 hours (hourly reset) |
| Login lockout | 60 minutes |
| API Lambda timeout | 30 seconds |
| Scraper Lambda timeout | 120 seconds |
| Svix webhook replay window | ±5 minutes |

### 13.4 Inbound Email Spam Filters

| Filter | Criteria |
|---|---|
| HMAC signature | Svix signature must verify; timestamp within ±5 min |
| Destination address | Must match configured `INBOUND_EMAIL_ADDR` |
| Subject keywords | Must contain one of: "my list", "publix", "shopping list", "grocery list" |
| Sender rate limit | Max 5 per sender per hour |
| User match | Sender must be a registered user (login email or notify_email) |

---

## 14. Security Changelog (v9.0 → v9.1)

Session 21 security hardening. All items deployed and verified via live vulnerability scan (46 requests, 35/41 pass, 0 real failures).

| # | Severity | Change |
|---|---|---|
| 1 | CRITICAL | Deleted `lambda/api.py` — 1,667 lines of dead legacy code containing reverted security fixes from every session since v4 |
| 2 | CRITICAL | Added PIN denylist (20 common PINs) enforced at registration, PIN change, and admin reset |
| 3 | CRITICAL | Rate-limited `/auth/register` to 5 per IP per hour |
| 4 | CRITICAL | Rate-limited `/auth/resend-verification` to 3 per email per hour |
| 5 | HIGH | Moved admin session token from `sessionStorage` (XSS-accessible) to `HttpOnly` cookie |
| 6 | HIGH | Added Svix webhook timestamp validation (±5 min) to prevent replay attacks |
| 7 | HIGH | Eliminated email enumeration: registration returns generic message; login removes attempt counter from errors |
| 8 | HIGH | Restricted CSP `connect-src` from wildcard `*` to `'self' https://cartwise.shopping https://*.cartwise.shopping` |
| 9 | HIGH | Removed `'unsafe-inline'` from CSP `script-src` — all 110 inline event handlers migrated to `data-action` attribute delegation with `addEventListener` |
| 10 | HIGH | Changed session cookie from `SameSite=None` to `SameSite=Lax; Domain=cartwise.shopping` |
| 11 | MEDIUM | Removed raw `AdminSecret` header fallback — admin auth is session-token-only |
| 12 | MEDIUM | Added API Gateway throttling (20 rps, burst 50) via `deploy.sh` |
| 13 | MEDIUM | Escaped all API-sourced data in `innerHTML` assignments with `escH()` |
| 14 | MEDIUM | Admin `patch_prefs` now filters to `ALLOWED_PREF_KEYS` (was accepting arbitrary keys) |
| 15 | MEDIUM | Rate-limited `send_test_email` (6/hr) and `resend_weekly_email` (3/hr) |
| 16 | MEDIUM | Locked `notify_email` to login email (prevents email relay abuse) |
| 17 | LOW | Sessions invalidated on PIN change (sets `valid_after`); frontend signs user out with 1.5s delay |
| 18 | LOW | Removed Resend API key prefix from error logs (was leaking first 8 chars) |
| 19 | LOW | Removed wildcard `Access-Control-Allow-Origin: *` from unsubscribe HTML pages |
| 20 | LOW | Added `Referrer-Policy` and `Permissions-Policy` security headers |

---

## 15. Environments & Release Process

| | Production | Staging |
|---|---|---|
| Folder | `C:\Projects\Cartwise\production` (git, `origin` = github.com/bjmoore123/Cartwise) | `C:\Projects\Cartwise\staging` (no git) |
| Resource prefix | `cartwise-*` | `cartwise-staging-*` |
| Hosts | `cartwise.shopping`, `api.cartwise.shopping` | `staging.cartwise.shopping`, `api-staging.cartwise.shopping` |
| Cookies | `session` / `admin_session` | `session_stg` / `admin_session_stg` |

The source files are identical in both folders; `deploy.sh` reads `ENV=staging` and injects the API URL at upload. Secrets (`ADMIN_SECRET`, `UNSUB_SECRET`, `RESEND_WEBHOOK_SECRET`) are reused from the live API Lambda when not passed; the Resend keys come from SSM (`/publix/resend-api-key`, optional `/publix/resend-receiving-key`).

**Release flow:** build and test (pytest, browser checks) → deploy staging → manual checks from the release's `docs/vX.Y-test-plan.md` → approval → copy into production, commit, deploy, push. See `docs/promotion-checklist.md` for the commands, hotfix back-porting and rollback.

