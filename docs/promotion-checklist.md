# CartWise: Staging → Production Promotion Checklist

**Folders:** `C:\Projects\Cartwise\staging` (no git) → `C:\Projects\Cartwise\production` (git, `origin` = github.com/bjmoore123/Cartwise)
**One deploy script for both:** `deploy.sh` reads `ENV`. Unset means production; `ENV=staging` targets the `cartwise-staging-*` resources, `staging.cartwise.shopping` and `api-staging.cartwise.shopping`.

## What differs per environment (handled by `deploy.sh`, not by code)

| Setting | Production | Staging |
|---|---|---|
| Resource prefix (`APP_NAME`) | `cartwise` | `cartwise-staging` |
| Site / API | `cartwise.shopping` (+ `www`) / `api.cartwise.shopping` | `staging.cartwise.shopping` / `api-staging.cartwise.shopping` |
| API URL in `app.js` | Placeholder `https://YOUR_API_GATEWAY_URL`, replaced at upload | same mechanism |
| Session / admin cookie names | `session` / `admin_session` | `session_stg` / `admin_session_stg` |
| Email sender name | `Cartwise Alerts` | `[STAGING] Cartwise Alerts` |
| SSM secrets | `/publix/*` (shared) | `/publix/*` (shared) |

The source files are therefore identical between environments. **Never commit a real API URL into `frontend/app.js`.** Keep the `YOUR_API_GATEWAY_URL` placeholder.

## Develop and test on staging

```bash
cd /c/Projects/Cartwise/staging
python -m pytest -q tests          # all green
ENV=staging APP_DOMAIN=cartwise.shopping ACM_CERT_ARN="<wildcard cert ARN>" RESEND_API_KEY="re_..." bash deploy.sh
```

Smoke test at https://staging.cartwise.shopping:
- [ ] Sign in, and sign in with a validation error first (button must recover)
- [ ] Deals load (core + TPR), matches appear, deal modal opens with chart
- [ ] Admin (`#admin`) loads and logs show
- [ ] HAR: no API call over 2 s warm; `/deals/history` under 100 KB
- [ ] Production session still valid in another tab (cookie names don't collide)

## Promote

```bash
# 1. See exactly what will change
diff -rq --exclude=.git --exclude=__pycache__ /c/Projects/Cartwise/staging /c/Projects/Cartwise/production

# 2. Copy staging over production (keeps production's .git)
cd /c/Projects/Cartwise
cp -r staging/. production/

# 3. Review, test, commit
cd production
git status && git diff --stat
python -m pytest -q tests
git add -A && git commit -m "Promote staging: <summary>"

# 4. Deploy production and push
APP_DOMAIN=cartwise.shopping ACM_CERT_ARN="<cert ARN>" RESEND_API_KEY="re_..." bash deploy.sh
git push
```

Then re-run the smoke test against https://cartwise.shopping.

## Hotfix straight to production (exception path)

For urgent production-only fixes (for example the v9.2 Batch 1 performance fix):
1. Fix in `production`, test, commit, deploy, push.
2. **Back-port the same change into `staging`** right away, or the next promotion will silently revert it.

## Rollback

```bash
cd /c/Projects/Cartwise/production
git log --oneline -5
git checkout <last-good-commit> -- .   # restore files only; history kept
APP_DOMAIN=cartwise.shopping ACM_CERT_ARN="<cert ARN>" bash deploy.sh
git commit -am "Rollback to <last-good-commit>" && git push
```
