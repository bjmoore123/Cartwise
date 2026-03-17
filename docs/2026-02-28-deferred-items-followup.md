# Code Review Remediation — Deferred Items Follow-Up Plan

**Date:** 2026-02-28
**Context:** Items from `code-review-2026-02-28.md` deferred because they require a domain name and/or SSL certificate that are not yet in place. Implement these once CloudFront + ACM setup is complete.

---

## Prerequisites

Before implementing any item in this document:

1. **Register or designate a domain** (e.g., `publix.yourdomain.com`)
2. **Request an ACM certificate** in `us-east-1` (required for CloudFront):
   ```bash
   aws acm request-certificate \
     --domain-name publix.yourdomain.com \
     --validation-method DNS \
     --region us-east-1
   ```
3. **Add the DNS CNAME validation record** and wait for `ISSUED` status
4. **Note your CloudFront distribution URL** (created in Phase 3 below)

---

## PDC-04 — Admin Session Tokens (HIGH)

**Finding:** Admin secret is stored raw in `sessionStorage` in the browser. If XSS occurs, the full admin secret is exposed.

**Deferred because:** The frontend is currently served from an S3 URL without HTTPS. HttpOnly cookies (PDC-05) and proper session scoping require HTTPS to be effective.

**Implementation plan:**

### Backend (`lambda/api.py`)

1. Add a `POST /admin/login` endpoint that validates the admin secret and returns a short-lived session token:

```python
ADMIN_SESSION_TTL_HOURS = 1

def admin_login(body):
    """POST /admin/login — exchange admin secret for a session token."""
    secret = (body.get("secret") or "").strip()
    if not ADMIN_SECRET or not hmac.compare_digest(secret, ADMIN_SECRET):
        return err("Unauthorized.", 401)
    token = secrets.token_hex(32)
    expires_at = int((datetime.now(timezone.utc) +
                      timedelta(hours=ADMIN_SESSION_TTL_HOURS)).timestamp())
    sessions_table.put_item(Item={
        "token": token,
        "email": "__admin__",
        "is_admin": True,
        "expires_at": expires_at,
        "session_created_at": int(datetime.now(timezone.utc).timestamp()),
    })
    return ok({"token": token, "expires_in": ADMIN_SESSION_TTL_HOURS * 3600})
```

2. Update `get_admin_auth()` to also accept an admin Bearer token:

```python
def get_admin_auth(event) -> bool:
    if not ADMIN_SECRET: return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "").strip()

    # Accept admin session token (Bearer prefix)
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        item = sessions_table.get_item(Key={"token": token}).get("Item")
        if item and item.get("is_admin") and \
           item.get("expires_at", 0) > int(datetime.now(timezone.utc).timestamp()):
            return True
        return False

    # Accept AdminSecret header (for backwards compat during migration)
    for prefix in ("AdminSecret ", "adminsecret "):
        if auth.lower().startswith(prefix.lower()):
            candidate = auth[len(prefix):].strip()
            return hmac.compare_digest(candidate, ADMIN_SECRET)
    return False
```

3. Add route to router:
```python
if path == "/admin/login" and method == "POST": return admin_login(body)
```

### Frontend (`frontend/app.js`)

1. In `adminLogin()`, call `POST /admin/login` with the secret and store the returned token (not the secret) in `sessionStorage`:

```javascript
async function adminLogin() {
  const secret = document.getElementById('admin-secret-inp').value.trim();
  try {
    const res = await fetch(API + '/admin/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({secret})
    });
    const data = await res.json();
    if (!res.ok) { showAdminMsg(data.error || 'Invalid secret'); return; }
    // Store session token, not the raw secret
    sessionStorage.setItem('cw_admin_token', data.token);
    // Clear the secret from the input
    document.getElementById('admin-secret-inp').value = '';
    adminDash();
  } catch(e) { showAdminMsg('Login failed'); }
}
```

2. All admin API calls should send `Authorization: Bearer <admin_token>` instead of `AdminSecret <secret>`.

3. `adminSignOut()` should clear `cw_admin_token` from sessionStorage.

---

## PDC-05 — HttpOnly Session Cookies (HIGH)

**Finding:** Session tokens stored in `localStorage` are accessible to JavaScript. XSS can steal them. HttpOnly cookies prevent JS access entirely.

**Deferred because:** `SameSite=Strict; Secure` cookies require HTTPS. Setting them over HTTP is ineffective.

**Implementation plan:**

### Backend (`lambda/api.py`)

1. In `login()`, add `Set-Cookie` header to the response:

```python
cookie = (
    f"session={token}; HttpOnly; Secure; SameSite=Strict; "
    f"Max-Age={SESSION_TTL_HOURS * 3600}; Path=/"
)
return {
    "statusCode": 200,
    "headers": {**CORS, "Set-Cookie": cookie},
    "body": json.dumps({"token": token, "email": email, "prefs": prefs}, cls=_Enc)
}
```

2. Update `get_session()` to also read from cookies (with Bearer fallback for migration):

```python
def get_session(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    # Try Bearer token first (migration fallback)
    token = headers.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    # Then try HttpOnly cookie
    if not token:
        cookie_header = headers.get("cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
                break
    if not token: return None
    # ... rest of session validation unchanged
```

3. Add `Access-Control-Allow-Credentials: true` to CORS:

```python
CORS = {
    "Access-Control-Allow-Origin":  FRONTEND_URL,  # Must not be * with credentials
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Credentials": "true",
    "Content-Type": "application/json",
}
```

4. In `logout()`, expire the cookie:

```python
def logout(event):
    sess = get_session(event)
    if sess:
        sessions_table.delete_item(Key={"token": sess.get("token", "")})
        try:
            users_table.update_item(...)  # valid_after (PDC-18, already done)
        except Exception as e:
            print(f"[CW] logout valid_after update failed: {e}")
    # Expire the cookie
    expired_cookie = "session=; HttpOnly; Secure; SameSite=Strict; Max-Age=0; Path=/"
    return {
        "statusCode": 200,
        "headers": {**CORS, "Set-Cookie": expired_cookie},
        "body": json.dumps({"message": "Logged out."})
    }
```

### Frontend (`frontend/app.js`)

Once cookies are working, remove `localStorage` token storage:
- Remove `localStorage.setItem('pdc_token', token)` from login handler
- Remove `localStorage.getItem('pdc_token')` from startup
- Remove `localStorage.removeItem('pdc_token')` from logout
- API calls that currently send `Authorization: Bearer <token>` can drop the header (cookies send automatically), but keeping the header during transition is harmless

**Note:** CORS must have `Access-Control-Allow-Origin` set to the exact CloudFront origin (not `*`) for `credentials: 'include'` to work. Update `fetch()` calls to add `credentials: 'include'` once cookies are in use.

---

## Phase 3.2 — CloudFront + HTTPS (PDC-13, PDC-14)

**Finding PDC-13:** App served over HTTP — tokens and PINs transmitted in plaintext.  
**Finding PDC-14:** S3 bucket is publicly accessible — should be restricted via OAI.

**Implementation plan:**

### Step 1 — Deploy CloudFront distribution

Add to `deploy.sh` after the S3 frontend upload step:

```bash
# ── CloudFront ────────────────────────────────────────────────────────────────
echo "▶  CloudFront distribution"

CERT_ARN="arn:aws:acm:us-east-1:${AWS_ACCOUNT_ID}:certificate/YOUR-CERT-ID"
DOMAIN="publix.yourdomain.com"

CF_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${APP_NAME}'].Id" \
  --output text 2>/dev/null)

if [ -z "$CF_ID" ] || [ "$CF_ID" = "None" ]; then
  CF_ID=$(aws cloudfront create-distribution --distribution-config '{
    "Comment": "'"${APP_NAME}"'",
    "Origins": {
      "Quantity": 1,
      "Items": [{
        "Id": "s3-origin",
        "DomainName": "'"${APP_NAME}"'.s3.amazonaws.com",
        "S3OriginConfig": {"OriginAccessIdentity": "origin-access-identity/cloudfront/'"${OAI_ID}"'"}
      }]
    },
    "DefaultCacheBehavior": {
      "TargetOriginId": "s3-origin",
      "ViewerProtocolPolicy": "redirect-to-https",
      "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
      "AllowedMethods": {"Quantity": 2, "Items": ["HEAD","GET"]}
    },
    "DefaultRootObject": "index.html",
    "Enabled": true,
    "ViewerCertificate": {
      "ACMCertificateArn": "'"${CERT_ARN}"'",
      "SSLSupportMethod": "sni-only",
      "MinimumProtocolVersion": "TLSv1.2_2021"
    },
    "Aliases": {"Quantity": 1, "Items": ["'"${DOMAIN}"'"]}
  }' --query 'Distribution.Id' --output text)
  echo "CloudFront distribution created: ${CF_ID}"
fi
```

### Step 2 — Create Origin Access Identity (OAI)

```bash
OAI_ID=$(aws cloudfront create-cloud-front-origin-access-identity \
  --cloud-front-origin-access-identity-config \
  CallerReference="${APP_NAME}",Comment="${APP_NAME}" \
  --query 'CloudFrontOriginAccessIdentity.Id' --output text)
```

### Step 3 — Update S3 bucket policy to OAI-only

Remove public access block after restricting to OAI:
```bash
aws s3api put-bucket-policy --bucket "${APP_NAME}" --policy '{
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"CanonicalUser": "'"${OAI_CANONICAL_ID}"'"},
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::'"${APP_NAME}"'/*"
  }]
}'
aws s3api put-public-access-block --bucket "${APP_NAME}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### Step 4 — Update environment variables

```bash
# In deploy.sh env var section:
FRONTEND_URL="https://${DOMAIN}"
# Update Lambda env:
aws lambda update-function-configuration \
  --function-name "${LAMBDA_API}" \
  --environment "Variables={...,FRONTEND_URL=https://${DOMAIN}}"
```

### Step 5 — Update CSP in index.html

Once API URL is stable, replace the `connect-src *` in the CSP with the specific API Gateway URL:
```html
connect-src https://YOUR-API-ID.execute-api.us-east-1.amazonaws.com;
```

---

## Summary Checklist

| Item | Prerequisite | Effort |
|------|-------------|--------|
| PDC-04 Admin session tokens | None (can do now) | ~1 hour |
| PDC-05 HttpOnly cookies | HTTPS / CloudFront live | ~1 hour |
| PDC-13 HTTPS enforcement | Domain + ACM cert | ~2 hours (deploy.sh) |
| PDC-14 S3 OAI (no public bucket) | CloudFront live | ~30 min |
| CSP `connect-src` tighten | API URL stable | ~5 min |

**Recommended order:** Get CloudFront + ACM set up first (PDC-13/14), then implement PDC-05 cookies, then PDC-04 admin sessions. Once cookies are in place, the admin session token feature (PDC-04) becomes much more valuable.
