#!/bin/bash
# =============================================================================
#  deploy.sh — Cartwise v9 (Fully Serverless)
#  Idempotent: safe to re-run. Everything existing is skipped or updated.
#
#  Basic usage (no custom domain):
#    RESEND_API_KEY="re_xxx" ADMIN_SECRET="yourpassword" bash deploy.sh
#
#  With CloudFront + custom domain (Phase 3+):
#    Set ACM_CERT_ARN and APP_DOMAIN before running. Phases 1 & 2 of the
#    domain/SSL plan must be complete (domain registered, cert ISSUED) before
#    these variables are available.
#
#    ACM_CERT_ARN="arn:aws:acm:us-east-1:ACCOUNT:certificate/YOUR-CERT-ID" \
#    APP_DOMAIN="cartwise.shopping" \
#    RESEND_API_KEY="re_xxx" ADMIN_SECRET="yourpassword" bash deploy.sh
# =============================================================================

export MSYS_NO_PATHCONV=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR_PY="$(cygpath -w "${SCRIPT_DIR}" 2>/dev/null || echo "${SCRIPT_DIR}")"
TMPDIR_PY="$(cygpath -w "$(mktemp -d)" 2>/dev/null || mktemp -d)"
win_path() { cygpath -w "$1" 2>/dev/null || echo "$1"; }

AWS_REGION="us-east-1"
APP_NAME="cartwise"
RESEND_FROM_NAME="Cartwise Alerts"
RESEND_FROM_ADDR="onboarding@resend.dev"

# ── CloudFront / custom domain (optional — Phase 3+) ──────────────────────────
# Leave these unset to deploy without CloudFront (S3 HTTP only).
# Set both to activate Phases 9-15 of this script.
#   ACM_CERT_ARN  — ARN of an ISSUED ACM certificate in us-east-1
#   APP_DOMAIN    — apex domain, e.g. cartwise.shopping
ACM_CERT_ARN="${ACM_CERT_ARN:-}"
APP_DOMAIN="${APP_DOMAIN:-}"
CF_LOG_BUCKET="${APP_NAME}-cf-logs"
CF_LOG_PREFIX="cf/"
CF_COMMENT="${APP_NAME}"   # used to find existing distribution by Comment field

if [ -z "${RESEND_API_KEY}" ]; then
  echo "ERROR: RESEND_API_KEY is not set."
  echo "Usage: RESEND_API_KEY=\"re_xxx\" ADMIN_SECRET=\"yourpassword\" bash deploy.sh"
  exit 1
fi

if [ -z "${ADMIN_SECRET}" ]; then
  ADMIN_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))" 2>/dev/null || cat /dev/urandom | head -c 24 | base64 | tr -d '=+/')
  echo "   ℹ️  No ADMIN_SECRET set — generated: ${ADMIN_SECRET}"
  echo "   Save this — you'll need it to access the Admin panel."
fi

# UNSUB_SECRET: salt for HMAC-based unsubscribe tokens. Auto-generated if not set.
# Rotating this value invalidates all existing unsubscribe links.
if [ -z "${UNSUB_SECRET}" ]; then
  UNSUB_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || cat /dev/urandom | head -c 32 | base64 | tr -d '=+/\n' | head -c 64)
  echo "   ℹ️  No UNSUB_SECRET set — generated automatically."
  echo "   To keep unsubscribe links stable across re-deploys, set UNSUB_SECRET=... before running deploy.sh"
fi

# INBOUND_EMAIL_ADDR: Resend inbound address for list-import emails.
# Change only this variable if the Resend address ever rotates.
INBOUND_EMAIL_ADDR="${INBOUND_EMAIL_ADDR:-cartwise@minaushii.resend.app}"

# RESEND_WEBHOOK_SECRET: Svix signing secret from Resend Webhooks dashboard.
# Must be set manually after registering the webhook. Lambda returns 400 until set.
if [ -z "${RESEND_WEBHOOK_SECRET}" ]; then
  echo "   ⚠️  RESEND_WEBHOOK_SECRET not set — inbound webhook will reject all requests."
  echo "   Obtain it from Resend dashboard after webhook registration, then re-deploy."
  RESEND_WEBHOOK_SECRET="not-yet-configured"
fi


AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
S3_FRONTEND="${APP_NAME}-frontend-${AWS_ACCOUNT_ID}"
# Use CloudFront custom domain if configured; S3 website URL as pre-CloudFront fallback
if [ -n "${APP_DOMAIN}" ]; then
  FRONTEND_URL="https://${APP_DOMAIN}"
else
  FRONTEND_URL="http://${S3_FRONTEND}.s3-website-${AWS_REGION}.amazonaws.com"
fi

ok()     { echo "   ✓ $*"; }
skip()   { echo "   - $* (already exists)"; }
info()   { echo "   → $*"; }
exists() { "$@" > /dev/null 2>&1; }

# apply_env FUNCTION_NAME KEY VALUE KEY VALUE ...
# Writes Lambda env vars via a Python-generated JSON file so special chars
# ($, %, spaces) in values are never interpreted by the shell.
apply_env() {
  local fn="$1"; shift
  local env_file="${TMPDIR_PY}/env_${fn//[^a-zA-Z0-9]/_}.json"
  python3 -c "
import json, sys
args = sys.argv[1:]
it = iter(args)
variables = {k: v for k, v in zip(it, it)}
print(json.dumps({'Variables': variables}))
" "$@" > "${env_file}"
  local win_env_file
  win_env_file="$(cygpath -m "${env_file}" 2>/dev/null || echo "${env_file}")"
  MSYS_NO_PATHCONV=1 aws lambda update-function-configuration \
    --function-name "${fn}" \
    --environment "file://${win_env_file}" \
    --region "${AWS_REGION}" > /dev/null
}

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Cartwise v9 — Serverless Deployment     ║"
echo "║   Account: ${AWS_ACCOUNT_ID}   Region: ${AWS_REGION} ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── STEP 1: DynamoDB tables ───────────────────────────────────────────────────
echo "▶  1/15  DynamoDB tables"

create_table() {
  local name="$1" pk="$2" pktype="${3:-S}"
  if ! exists aws dynamodb describe-table --table-name "${name}" --region "${AWS_REGION}"; then
    aws dynamodb create-table --table-name "${name}" \
      --attribute-definitions AttributeName="${pk}",AttributeType="${pktype}" \
      --key-schema AttributeName="${pk}",KeyType=HASH \
      --billing-mode PAY_PER_REQUEST --region "${AWS_REGION}" > /dev/null
    ok "DynamoDB table: ${name}"
  else
    skip "DynamoDB table: ${name}"
  fi
}

create_table "${APP_NAME}-users"          "email"
create_table "${APP_NAME}-sessions"       "token"
create_table "${APP_NAME}-admin-sessions" "token"
create_table "${APP_NAME}-deals"          "store_id"
create_table "${APP_NAME}-scrape-logs"    "job_id"
create_table "${APP_NAME}-auth-logs"      "log_id"
create_table "${APP_NAME}-app-logs"       "log_id"
create_table "${APP_NAME}-deal-history"   "store_id"
create_table "${APP_NAME}-deal-corpus"    "corpus_id"

# Enable TTL on deal-history (expires_at attribute)
# Wait for table to reach ACTIVE status before attempting TTL update
if exists aws dynamodb describe-table --table-name "${APP_NAME}-deal-history" --region "${AWS_REGION}"; then
  TTL_STATUS=$(aws dynamodb describe-time-to-live \
    --table-name "${APP_NAME}-deal-history" --region "${AWS_REGION}" \
    --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null)
  if [ "${TTL_STATUS}" != "ENABLED" ]; then
    info "Waiting for deal-history table to become ACTIVE..."
    aws dynamodb wait table-exists --table-name "${APP_NAME}-deal-history" --region "${AWS_REGION}"
    TABLE_STATUS=""
    for i in $(seq 1 20); do
      TABLE_STATUS=$(aws dynamodb describe-table \
        --table-name "${APP_NAME}-deal-history" --region "${AWS_REGION}" \
        --query 'Table.TableStatus' --output text 2>/dev/null)
      [ "${TABLE_STATUS}" = "ACTIVE" ] && break
      info "  Table status: ${TABLE_STATUS} — waiting 5s..."
      sleep 5
    done
    if [ "${TABLE_STATUS}" = "ACTIVE" ]; then
      aws dynamodb update-time-to-live \
        --table-name "${APP_NAME}-deal-history" \
        --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
        --region "${AWS_REGION}" > /dev/null
      ok "TTL enabled on deal-history (expires_at)"
    else
      echo "   ⚠️  deal-history table did not reach ACTIVE in time — TTL not set. Re-run deploy.sh to retry."
    fi
  else
    skip "TTL on deal-history (already enabled)"
  fi
fi

# Enable TTL on remaining tables (sessions, deals, auth-logs, app-logs)
# All use expires_at attribute; idempotent — safe to re-run
enable_ttl() {
  local tbl="${APP_NAME}-$1"
  if exists aws dynamodb describe-table --table-name "${tbl}" --region "${AWS_REGION}"; then
    local status
    status=$(aws dynamodb describe-time-to-live \
      --table-name "${tbl}" --region "${AWS_REGION}" \
      --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null)
    if [ "${status}" != "ENABLED" ]; then
      aws dynamodb update-time-to-live \
        --table-name "${tbl}" \
        --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
        --region "${AWS_REGION}" > /dev/null 2>&1 \
        && ok "TTL enabled on ${tbl} (expires_at)" \
        || echo "   ⚠️  TTL on ${tbl} not set (table may still be creating — re-run to retry)"
    else
      skip "TTL on ${tbl} (already enabled)"
    fi
  fi
}

enable_ttl "sessions"
enable_ttl "admin-sessions"
enable_ttl "deals"
enable_ttl "auth-logs"
enable_ttl "app-logs"
enable_ttl "scrape-logs"

# ── STEP 2: IAM role ──────────────────────────────────────────────────────────
echo "▶  2/15  IAM role"

LAMBDA_ROLE="${APP_NAME}-lambda-role"
if ! exists aws iam get-role --role-name "${LAMBDA_ROLE}"; then
  aws iam create-role --role-name "${LAMBDA_ROLE}" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /dev/null
  aws iam attach-role-policy --role-name "${LAMBDA_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  ok "IAM role: ${LAMBDA_ROLE}"
  info "Waiting 10s for role to propagate..."
  sleep 10
fi

# Always refresh inline policy to ensure new table permissions are present
aws iam put-role-policy --role-name "${LAMBDA_ROLE}" \
  --policy-name "${APP_NAME}-lambda-policy" \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
    {\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:PutItem\",\"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\",\"dynamodb:Scan\",\"dynamodb:Query\"],
     \"Resource\":[
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-users\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-sessions\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-admin-sessions\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-deals\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-scrape-logs\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-auth-logs\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-app-logs\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-deal-history\",
       \"arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${APP_NAME}-deal-corpus\"
     ]},
    {\"Effect\":\"Allow\",\"Action\":[\"lambda:InvokeFunction\"],
     \"Resource\":\"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${APP_NAME}-scraper\"},
    {\"Effect\":\"Allow\",\"Action\":[\"logs:DescribeLogStreams\",\"logs:GetLogEvents\",\"logs:FilterLogEvents\"],
     \"Resource\":\"arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/lambda/${APP_NAME}-scraper:*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"acm:DescribeCertificate\"],
     \"Resource\":\"arn:aws:acm:us-east-1:${AWS_ACCOUNT_ID}:certificate/*\"},
    {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],
     \"Resource\":[\"arn:aws:s3:::${CF_LOG_BUCKET}\",\"arn:aws:s3:::${CF_LOG_BUCKET}/*\"]}
  ]}" > /dev/null
skip "IAM role: ${LAMBDA_ROLE} (policy refreshed)"

LAMBDA_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE}"

# ── STEP 3: API Lambda ────────────────────────────────────────────────────────
echo "▶  3/15  API Lambda"

LAMBDA_API="${APP_NAME}-api"
LAMBDA_API_ZIP="${TMPDIR_PY}/lambda_api.zip"

if [ ! -f "${SCRIPT_DIR}/lambda/handler.py" ]; then echo "ERROR: lambda/handler.py not found"; exit 1; fi

PIP_CMD=$(which pip3 2>/dev/null || which pip 2>/dev/null || echo "python3 -m pip")

API_BUILD_TMP="${TMPDIR_PY}/api_build"
rm -rf "${API_BUILD_TMP}"; mkdir -p "${API_BUILD_TMP}"
# Copy all lambda modules (Architecture #1: modular split)
for f in handler.py helpers.py auth.py prefs.py deals.py admin.py inbound.py logging_utils.py; do
  cp "${SCRIPT_DIR}/lambda/${f}" "${API_BUILD_TMP}/"
done
# Include shared libraries (deal_parser + matcher)
mkdir -p "${API_BUILD_TMP}/shared"
cp "${SCRIPT_DIR}/shared/__init__.py"   "${API_BUILD_TMP}/shared/"
cp "${SCRIPT_DIR}/shared/deal_parser.py" "${API_BUILD_TMP}/shared/"
cp "${SCRIPT_DIR}/shared/matcher.py"    "${API_BUILD_TMP}/shared/"
info "Installing api dependencies (resend)..."
${PIP_CMD} install resend \
  --target "${API_BUILD_TMP}" --quiet \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: 2>/dev/null || \
${PIP_CMD} install resend --target "${API_BUILD_TMP}" --quiet
python3 -c "
import zipfile, os
build_dir=r'${API_BUILD_TMP}'
dst=r'${LAMBDA_API_ZIP}'
with zipfile.ZipFile(dst,'w',zipfile.ZIP_DEFLATED) as z:
    for root,dirs,files in os.walk(build_dir):
        dirs[:]=[d for d in dirs if d not in ['__pycache__']]
        for f in files:
            if f.endswith('.pyc'): continue
            fp=os.path.join(root,f)
            z.write(fp,os.path.relpath(fp,build_dir))
print('API zip: '+dst)
" || { echo "ERROR: Failed to create API zip"; exit 1; }

# Resolve Resend API key — prefer SSM (survives redeploys without re-passing key on CLI)
# Falls back to shell env var if SSM param not yet set.
RESEND_KEY_VAL=$(aws ssm get-parameter --name "/publix/resend-api-key" \
  --with-decryption --query Parameter.Value --output text --region "${AWS_REGION}" 2>/dev/null || echo "${RESEND_API_KEY}")

# env applied via apply_env (supports special chars in ADMIN_SECRET)

if ! exists aws lambda get-function --function-name "${LAMBDA_API}" --region "${AWS_REGION}"; then
  aws lambda create-function \
    --function-name "${LAMBDA_API}" \
    --runtime python3.12 \
    --role "${LAMBDA_ROLE_ARN}" \
    --handler handler.handler \
    --zip-file "fileb://$(win_path "${LAMBDA_API_ZIP}")" \
    --timeout 30 \
    --memory-size 256 \
    --region "${AWS_REGION}" > /dev/null
  aws lambda wait function-active --function-name "${LAMBDA_API}" --region "${AWS_REGION}"
  apply_env "${LAMBDA_API}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "SESSIONS_TABLE" "${APP_NAME}-sessions" \
    "ADMIN_SESSIONS_TABLE" "${APP_NAME}-admin-sessions" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "AUTH_LOGS_TABLE" "${APP_NAME}-auth-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "ADMIN_SECRET" "${ADMIN_SECRET}" \
    "SCRAPER_FUNCTION" "${APP_NAME}-scraper" \
    "PDC_REGION" "${AWS_REGION}" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "INBOUND_EMAIL_ADDR" "${INBOUND_EMAIL_ADDR}" \
    "RESEND_WEBHOOK_SECRET" "${RESEND_WEBHOOK_SECRET}" \
    "ACM_CERT_ARN" "${ACM_CERT_ARN}" \
    "CF_LOG_BUCKET" "${CF_LOG_BUCKET}" \
    "CF_LOG_PREFIX" "${CF_LOG_PREFIX}"
  ok "Lambda created: ${LAMBDA_API}"
else
  aws lambda wait function-active --function-name "${LAMBDA_API}" --region "${AWS_REGION}" 2>/dev/null || true
  aws lambda update-function-code \
    --function-name "${LAMBDA_API}" \
    --zip-file "fileb://$(win_path "${LAMBDA_API_ZIP}")" \
    --region "${AWS_REGION}" > /dev/null
  aws lambda wait function-updated --function-name "${LAMBDA_API}" --region "${AWS_REGION}" 2>/dev/null || sleep 5
  aws lambda update-function-configuration \
    --function-name "${LAMBDA_API}" \
    --handler handler.handler \
    --region "${AWS_REGION}" > /dev/null
  apply_env "${LAMBDA_API}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "SESSIONS_TABLE" "${APP_NAME}-sessions" \
    "ADMIN_SESSIONS_TABLE" "${APP_NAME}-admin-sessions" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "AUTH_LOGS_TABLE" "${APP_NAME}-auth-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "ADMIN_SECRET" "${ADMIN_SECRET}" \
    "SCRAPER_FUNCTION" "${APP_NAME}-scraper" \
    "PDC_REGION" "${AWS_REGION}" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "INBOUND_EMAIL_ADDR" "${INBOUND_EMAIL_ADDR}" \
    "RESEND_WEBHOOK_SECRET" "${RESEND_WEBHOOK_SECRET}" \
    "ACM_CERT_ARN" "${ACM_CERT_ARN}" \
    "CF_LOG_BUCKET" "${CF_LOG_BUCKET}" \
    "CF_LOG_PREFIX" "${CF_LOG_PREFIX}"
  ok "Lambda updated: ${LAMBDA_API}"
fi

LAMBDA_API_ARN=$(aws lambda get-function --function-name "${LAMBDA_API}" --region "${AWS_REGION}" --query 'Configuration.FunctionArn' --output text)

# ── STEP 4: Scraper Lambda ────────────────────────────────────────────────────
echo "▶  4/15  Scraper Lambda"

LAMBDA_SCRAPER="${APP_NAME}-scraper"
LAMBDA_SCRAPER_ZIP="${TMPDIR_PY}/lambda_scraper.zip"

for f in main.py scraper.py; do
  [ ! -f "${SCRIPT_DIR}/scraper/${f}" ] && { echo "ERROR: Missing: ${SCRIPT_DIR}/scraper/${f}"; exit 1; }
done

SCRAPER_TMP="${TMPDIR_PY}/scraper_build"
rm -rf "${SCRAPER_TMP}"; mkdir -p "${SCRAPER_TMP}"
cp "${SCRIPT_DIR}/scraper/main.py"    "${SCRAPER_TMP}/"
cp "${SCRIPT_DIR}/scraper/scraper.py" "${SCRAPER_TMP}/"
# Include shared libraries (deal_parser + matcher)
mkdir -p "${SCRAPER_TMP}/shared"
cp "${SCRIPT_DIR}/shared/__init__.py"    "${SCRAPER_TMP}/shared/"
cp "${SCRIPT_DIR}/shared/deal_parser.py" "${SCRAPER_TMP}/shared/"
cp "${SCRIPT_DIR}/shared/matcher.py"     "${SCRAPER_TMP}/shared/"

info "Installing scraper dependencies..."
${PIP_CMD} install resend rapidfuzz \
  --target "${SCRAPER_TMP}" --quiet \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: 2>/dev/null || \
${PIP_CMD} install resend rapidfuzz --target "${SCRAPER_TMP}" --quiet

python3 -c "
import zipfile, os
build_dir=r'${SCRAPER_TMP}'
dst=r'${LAMBDA_SCRAPER_ZIP}'
with zipfile.ZipFile(dst,'w',zipfile.ZIP_DEFLATED) as z:
    for root,dirs,files in os.walk(build_dir):
        dirs[:]=[d for d in dirs if d not in ['__pycache__']]
        for f in files:
            if f.endswith('.pyc'): continue
            fp=os.path.join(root,f)
            z.write(fp,os.path.relpath(fp,build_dir))
print('Scraper zip: '+dst)
" || { echo "ERROR: Failed to create scraper zip"; exit 1; }

RESEND_KEY_VAL=$(aws ssm get-parameter --name "/publix/resend-api-key" \
  --with-decryption --query Parameter.Value --output text --region "${AWS_REGION}" 2>/dev/null || echo "${RESEND_API_KEY}")

# env applied via apply_env (see below)

if ! exists aws lambda get-function --function-name "${LAMBDA_SCRAPER}" --region "${AWS_REGION}"; then
  aws lambda create-function \
    --function-name "${LAMBDA_SCRAPER}" \
    --runtime python3.12 \
    --role "${LAMBDA_ROLE_ARN}" \
    --handler main.handler \
    --zip-file "fileb://$(win_path "${LAMBDA_SCRAPER_ZIP}")" \
    --timeout 300 \
    --memory-size 512 \
    --region "${AWS_REGION}" > /dev/null
  aws lambda wait function-active --function-name "${LAMBDA_SCRAPER}" --region "${AWS_REGION}"
  apply_env "${LAMBDA_SCRAPER}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "PDC_REGION" "${AWS_REGION}"
  ok "Lambda created: ${LAMBDA_SCRAPER}"
else
  aws lambda wait function-active --function-name "${LAMBDA_SCRAPER}" --region "${AWS_REGION}" 2>/dev/null || true
  aws lambda update-function-code \
    --function-name "${LAMBDA_SCRAPER}" \
    --zip-file "fileb://$(win_path "${LAMBDA_SCRAPER_ZIP}")" \
    --region "${AWS_REGION}" > /dev/null
  aws lambda wait function-updated --function-name "${LAMBDA_SCRAPER}" --region "${AWS_REGION}" 2>/dev/null || sleep 5
  apply_env "${LAMBDA_SCRAPER}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "PDC_REGION" "${AWS_REGION}"
  ok "Lambda updated: ${LAMBDA_SCRAPER}"
fi

LAMBDA_SCRAPER_ARN=$(aws lambda get-function --function-name "${LAMBDA_SCRAPER}" --region "${AWS_REGION}" --query 'Configuration.FunctionArn' --output text)

# ── STEP 5: SSM secrets (optional, for reference) ─────────────────────────────
echo "▶  5/15  SSM secrets"
put_ssm() {
  local name="$1" val="$2" type="${3:-SecureString}"
  if ! exists aws ssm get-parameter --name "/publix/${name}" --region "${AWS_REGION}"; then
    aws ssm put-parameter --name "/publix/${name}" --value "${val}" --type "${type}" --region "${AWS_REGION}" > /dev/null
    ok "SSM /publix/${name}"
  else
    skip "SSM /publix/${name}"
  fi
}
put_ssm "resend-api-key"   "${RESEND_API_KEY}"
put_ssm "resend-from-name" "${RESEND_FROM_NAME}" "String"
put_ssm "resend-from-addr" "${RESEND_FROM_ADDR}" "String"

# ── STEP 6: API Gateway ───────────────────────────────────────────────────────
echo "▶  6/15  API Gateway"

# Look up by custom domain mapping first (survives renames), then by name
if [ -n "${APP_DOMAIN}" ]; then
  API_ID=$(aws apigatewayv2 get-domain-name --domain-name "api.${APP_DOMAIN}" --region "${AWS_REGION}"     --query "ApiMappings" --output text 2>/dev/null || true)
  API_ID=$(aws apigatewayv2 get-api-mappings --domain-name "api.${APP_DOMAIN}" --region "${AWS_REGION}"     --query "Items[0].ApiId" --output text 2>/dev/null || true)
fi
if [ -z "${API_ID}" ] || [ "${API_ID}" = "None" ]; then
  API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}"     --query "Items[?contains(Name,'-api-gw')].ApiId" --output text 2>/dev/null | awk '{print $1}')
fi

if [ -z "${API_ID}" ] || [ "${API_ID}" = "None" ]; then
  API_ID=$(aws apigatewayv2 create-api \
    --name "${APP_NAME}-api-gw" \
    --protocol-type HTTP \
    --region "${AWS_REGION}" \
    --query 'ApiId' --output text)
  ok "API Gateway created: ${API_ID}"
fi

# Always set exact CORS origins (supports both apex and www)
APIGW_CORS_FILE="${TMPDIR_PY}/apigw_cors.json"
python3 -c "
import json, sys
api_id = sys.argv[1]
origins = [f'https://{sys.argv[2]}', f'https://www.{sys.argv[2]}']
cfg = {
  'ApiId': api_id,
  'CorsConfiguration': {
    'AllowOrigins':     origins,
    'AllowMethods':     ['GET','POST','PUT','DELETE','OPTIONS'],
    'AllowHeaders':     ['Content-Type','Authorization','Cookie'],
    'AllowCredentials': True,
  }
}
print(json.dumps(cfg))
" "${API_ID}" "${APP_DOMAIN:-localhost}" > "${APIGW_CORS_FILE}"
MSYS_NO_PATHCONV=1 aws apigatewayv2 update-api \
  --cli-input-json "file://$(cygpath -m "${APIGW_CORS_FILE}" 2>/dev/null || echo "${APIGW_CORS_FILE}")" \
  --region "${AWS_REGION}" > /dev/null

API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
# If custom domain is configured, use it for API_URL so app.js gets the right URL at upload time
if [ -n "${APP_DOMAIN}" ]; then
  API_URL="https://api.${APP_DOMAIN}"
fi

INTEG_ID=$(aws apigatewayv2 get-integrations \
  --api-id "${API_ID}" --region "${AWS_REGION}" \
  --query 'Items[0].IntegrationId' --output text 2>/dev/null)

if [ -z "${INTEG_ID}" ] || [ "${INTEG_ID}" = "None" ]; then
  INTEG_ID=$(aws apigatewayv2 create-integration \
    --api-id "${API_ID}" \
    --integration-type AWS_PROXY \
    --integration-uri "${LAMBDA_API_ARN}" \
    --payload-format-version 2.0 \
    --region "${AWS_REGION}" \
    --query 'IntegrationId' --output text)
  aws apigatewayv2 create-route \
    --api-id "${API_ID}" \
    --route-key "\$default" \
    --target "integrations/${INTEG_ID}" \
    --region "${AWS_REGION}" > /dev/null
  aws apigatewayv2 create-stage \
    --api-id "${API_ID}" \
    --stage-name "\$default" \
    --auto-deploy \
    --region "${AWS_REGION}" > /dev/null
  ok "API Gateway integration + route created"
else
  # Always update integration URI to point to current Lambda (handles Lambda renames)
  aws apigatewayv2 update-integration \
    --api-id "${API_ID}" \
    --integration-id "${INTEG_ID}" \
    --integration-uri "${LAMBDA_API_ARN}" \
    --region "${AWS_REGION}" > /dev/null
  ok "API Gateway integration (already exists)"
fi
# Always ensure Lambda invoke permission exists for this API GW (idempotent)
aws lambda add-permission \
  --function-name "${LAMBDA_API}" \
  --statement-id "apigateway-invoke" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || true
# Always update API GW name to match current app name
aws apigatewayv2 update-api \
  --api-id "${API_ID}" \
  --name "${APP_NAME}-api-gw" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || true

# PDC-21: API Gateway throttling — prevents DDoS / cost abuse
aws apigatewayv2 update-stage \
  --api-id "${API_ID}" \
  --stage-name '$default' \
  --default-route-settings '{"ThrottlingBurstLimit":50,"ThrottlingRateLimit":20}' \
  --region "${AWS_REGION}" > /dev/null 2>&1 \
  && ok "API Gateway throttling set (20 rps, burst 50)" \
  || echo "   ⚠️  Could not set API Gateway throttling — set manually if needed"

# ── STEP 7: Frontend ──────────────────────────────────────────────────────────
echo "▶  7/15  Frontend (S3 static site)"

if ! exists aws s3api head-bucket --bucket "${S3_FRONTEND}"; then
  aws s3api create-bucket --bucket "${S3_FRONTEND}" --region "${AWS_REGION}" > /dev/null
  aws s3api put-public-access-block --bucket "${S3_FRONTEND}" \
    --public-access-block-configuration \
    "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"
  aws s3api put-bucket-website --bucket "${S3_FRONTEND}" \
    --website-configuration '{"IndexDocument":{"Suffix":"index.html"}}'
  aws s3api put-bucket-policy --bucket "${S3_FRONTEND}" \
    --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::${S3_FRONTEND}/*\"}]}"
  ok "S3 bucket (frontend): ${S3_FRONTEND}"
else
  skip "S3 bucket (frontend): ${S3_FRONTEND}"
fi

python3 -c "
import re, os
src = open(r'${SCRIPT_DIR_PY}/frontend/index.html', encoding='utf-8').read()
out = src.replace('https://YOUR_API_GATEWAY_URL', '${API_URL}').replace('window.CARTWISE_API_URL', 'window.CARTWISE_API_URL')
open('index_deploy_tmp.html', 'w', encoding='utf-8').write(out)
js = open(r'${SCRIPT_DIR_PY}/frontend/app.js', encoding='utf-8').read()
js_out = js.replace('https://YOUR_API_GATEWAY_URL', '${API_URL}')
open('app_deploy_tmp.js', 'w', encoding='utf-8').write(js_out)
"
MSYS_NO_PATHCONV=1 aws s3 cp index_deploy_tmp.html \
  "s3://${S3_FRONTEND}/index.html" \
  --content-type "text/html" --cache-control "no-cache, no-store, must-revalidate" --no-progress
MSYS_NO_PATHCONV=1 aws s3 cp app_deploy_tmp.js \
  "s3://${S3_FRONTEND}/app.js" \
  --content-type "application/javascript" --cache-control "no-cache, no-store, must-revalidate" --no-progress


# Verify uploads succeeded by comparing local MD5 to S3 ETag
# S3 ETag for non-multipart uploads equals the MD5 of the uploaded bytes
python3 -c "
import hashlib, subprocess, sys
files = [('index_deploy_tmp.html','index.html'), ('app_deploy_tmp.js','app.js')]
ok = True
for local, key in files:
    local_md5 = hashlib.md5(open(local,'rb').read()).hexdigest()
    result = subprocess.run(
        ['aws','s3api','head-object','--bucket','${S3_FRONTEND}','--key',key,'--query','ETag','--output','text'],
        capture_output=True, text=True
    )
    remote_etag = result.stdout.strip().strip('\"')
    if local_md5 == remote_etag:
        print(f'  ✓ {key} upload verified ({local_md5[:8]}…)')
    else:
        print(f'  ✗ WARNING: {key} ETag mismatch — local {local_md5[:8]}… != remote {remote_etag[:8]}…', file=sys.stderr)
        ok = False
if not ok:
    print('  Re-run deploy.sh if frontend changes are not visible after a hard refresh.', file=sys.stderr)
"
rm -f index_deploy_tmp.html app_deploy_tmp.js
# Use CloudFront URL if domain is configured, otherwise S3 website URL as fallback
if [ -n "${APP_DOMAIN}" ]; then
  FRONTEND_URL="https://${APP_DOMAIN}"
  ok "Frontend deployed: ${FRONTEND_URL} (via CloudFront)"
else
  FRONTEND_URL="http://${S3_FRONTEND}.s3-website-${AWS_REGION}.amazonaws.com"
  ok "Frontend deployed: ${FRONTEND_URL}"
fi

# ── STEP 8: EventBridge ───────────────────────────────────────────────────────
echo "▶  8/15  EventBridge weekly trigger"

RULE_NAME="${APP_NAME}-weekly"
RULE_ARN=$(aws events put-rule \
  --name "${RULE_NAME}" \
  --schedule-expression "cron(0 12 ? * WED *)" \
  --state ENABLED \
  --description "Cartwise — weekly scraper" \
  --region "${AWS_REGION}" \
  --query 'RuleArn' --output text)
ok "EventBridge rule: ${RULE_NAME}"

aws lambda add-permission \
  --function-name "${LAMBDA_SCRAPER}" \
  --statement-id "eventbridge-invoke" \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "${RULE_ARN}" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || true

aws events put-targets \
  --rule "${RULE_NAME}" \
  --targets "Id=scraper,Arn=${LAMBDA_SCRAPER_ARN}" \
  --region "${AWS_REGION}" > /dev/null
ok "EventBridge → scraper target set"

# ── STEP 8b: Monthly cleanup rule ───────────────────────────────────────────────
CLEANUP_RULE="${APP_NAME}-monthly-cleanup"
CLEANUP_ARN=$(aws events put-rule \
  --name "${CLEANUP_RULE}" \
  --schedule-expression "cron(0 13 1 * ? *)" \
  --state ENABLED \
  --description "Cartwise — monthly inactive-account cleanup" \
  --region "${AWS_REGION}" \
  --query 'RuleArn' --output text)
ok "EventBridge rule: ${CLEANUP_RULE}"

aws lambda add-permission \
  --function-name "${LAMBDA_SCRAPER}" \
  --statement-id "eventbridge-cleanup-invoke" \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "${CLEANUP_ARN}" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || true

aws events put-targets \
  --rule "${CLEANUP_RULE}" \
  --targets "Id=cleanup,Arn=${LAMBDA_SCRAPER_ARN},Input=\"{\\\"source\\\":\\\"monthly-cleanup\\\"}\"" \
  --region "${AWS_REGION}" > /dev/null
ok "EventBridge → cleanup target set (1st of month, 13:00 UTC)"

# ── Final env update: re-apply both Lambda configs now that API_URL and FRONTEND_URL are known ──
# On first deploy these were empty; this pass fills them in correctly.
# env applied via apply_env (supports special chars in ADMIN_SECRET)
# env applied via apply_env (see below)
apply_env "${LAMBDA_API}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "SESSIONS_TABLE" "${APP_NAME}-sessions" \
    "ADMIN_SESSIONS_TABLE" "${APP_NAME}-admin-sessions" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "AUTH_LOGS_TABLE" "${APP_NAME}-auth-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "ADMIN_SECRET" "${ADMIN_SECRET}" \
    "SCRAPER_FUNCTION" "${APP_NAME}-scraper" \
    "PDC_REGION" "${AWS_REGION}" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "INBOUND_EMAIL_ADDR" "${INBOUND_EMAIL_ADDR}" \
    "RESEND_WEBHOOK_SECRET" "${RESEND_WEBHOOK_SECRET}" \
    "ACM_CERT_ARN" "${ACM_CERT_ARN}" \
    "CF_LOG_BUCKET" "${CF_LOG_BUCKET}" \
    "CF_LOG_PREFIX" "${CF_LOG_PREFIX}"
aws lambda wait function-updated --function-name "${LAMBDA_API}" --region "${AWS_REGION}" 2>/dev/null || sleep 5
apply_env "${LAMBDA_SCRAPER}" \
    "FRONTEND_URL" "${FRONTEND_URL}" \
    "API_URL" "${API_URL}" \
    "UNSUB_SECRET" "${UNSUB_SECRET}" \
    "USERS_TABLE" "${APP_NAME}-users" \
    "DEALS_TABLE" "${APP_NAME}-deals" \
    "SCRAPE_LOGS_TABLE" "${APP_NAME}-scrape-logs" \
    "APP_LOGS_TABLE" "${APP_NAME}-app-logs" \
    "HISTORY_TABLE" "${APP_NAME}-deal-history" \
    "CORPUS_TABLE" "${APP_NAME}-deal-corpus" \
    "RESEND_API_KEY" "${RESEND_KEY_VAL}" \
    "RESEND_FROM_NAME" "${RESEND_FROM_NAME}" \
    "RESEND_FROM_ADDR" "${RESEND_FROM_ADDR}" \
    "PDC_REGION" "${AWS_REGION}"
ok "Lambda env vars updated with final URLs (FRONTEND_URL, API_URL, UNSUB_SECRET)"

# ── STEP 9–15: CloudFront + Custom Domain (Phase 3+) ─────────────────────────
# These steps are skipped if ACM_CERT_ARN or APP_DOMAIN are not set.
# Complete Phases 1 & 2 of the domain/SSL plan first, then re-run deploy.sh
# with ACM_CERT_ARN and APP_DOMAIN set as environment variables.

CF_DIST_ID=""   # will be populated in step 12 if CF exists or is created

if [ -z "${ACM_CERT_ARN}" ] || [ -z "${APP_DOMAIN}" ]; then
  echo "▶  9-15/15  CloudFront + domain — SKIPPED"
  info "Set ACM_CERT_ARN and APP_DOMAIN to activate CloudFront deployment."
  info "See domain/SSL planning doc Phases 1-2 for prerequisites."
else
  API_SUBDOMAIN="api.${APP_DOMAIN}"

  # ── STEP 9: S3 log bucket ──────────────────────────────────────────────────
  echo "▶  9/15  S3 CloudFront log bucket"

  if ! aws s3api head-bucket --bucket "${CF_LOG_BUCKET}" > /dev/null 2>&1; then
    aws s3api create-bucket \
      --bucket "${CF_LOG_BUCKET}" \
      --region "${AWS_REGION}" > /dev/null
    # Grant CloudFront log delivery write access
    # BucketOwnerPreferred is required before ACLs can be granted on new buckets
    aws s3api put-bucket-ownership-controls \
      --bucket "${CF_LOG_BUCKET}" \
      --ownership-controls '{"Rules":[{"ObjectOwnership":"BucketOwnerPreferred"}]}' > /dev/null
    aws s3api put-bucket-acl \
      --bucket "${CF_LOG_BUCKET}" \
      --acl log-delivery-write > /dev/null 2>&1 || true
    # Block all public access on the log bucket
    aws s3api put-public-access-block \
      --bucket "${CF_LOG_BUCKET}" \
      --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" > /dev/null
    # Expire logs after 90 days to control storage cost
    aws s3api put-bucket-lifecycle-configuration \
      --bucket "${CF_LOG_BUCKET}" \
      --lifecycle-configuration '{
        "Rules": [{
          "ID": "expire-cf-logs",
          "Status": "Enabled",
          "Filter": {"Prefix": ""},
          "Expiration": {"Days": 90}
        }]
      }' > /dev/null
    ok "S3 log bucket: ${CF_LOG_BUCKET}"
  else
    skip "S3 log bucket: ${CF_LOG_BUCKET}"
  fi

  # ── STEP 10: CloudFront Origin Access Control ──────────────────────────────
  echo "▶  10/15  CloudFront Origin Access Control (OAC)"

  OAC_NAME="${APP_NAME}-oac"
  OAC_ID=$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id" \
    --output text 2>/dev/null)

  if [ -z "${OAC_ID}" ] || [ "${OAC_ID}" = "None" ]; then
    OAC_ID=$(aws cloudfront create-origin-access-control \
      --origin-access-control-config "{
        \"Name\": \"${OAC_NAME}\",
        \"Description\": \"${APP_NAME} S3 OAC\",
        \"SigningProtocol\": \"sigv4\",
        \"SigningBehavior\": \"always\",
        \"OriginAccessControlOriginType\": \"s3\"
      }" \
      --query 'OriginAccessControl.Id' --output text)
    ok "CloudFront OAC: ${OAC_ID}"
  else
    skip "CloudFront OAC: ${OAC_ID}"
  fi

  # ── STEP 11: Response headers policy (security headers) ───────────────────
  echo "▶  11/15  CloudFront response headers policy"

  HEADERS_POLICY_NAME="${APP_NAME}-security-headers"
  HEADERS_POLICY_ID=$(aws cloudfront list-response-headers-policies \
    --type custom \
    --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='${HEADERS_POLICY_NAME}'].ResponseHeadersPolicy.Id" \
    --output text 2>/dev/null)

  if [ -z "${HEADERS_POLICY_ID}" ] || [ "${HEADERS_POLICY_ID}" = "None" ]; then
    HEADERS_POLICY_ID=$(aws cloudfront create-response-headers-policy \
      --response-headers-policy-config "{
        \"Name\": \"${HEADERS_POLICY_NAME}\",
        \"Comment\": \"Security headers for ${APP_DOMAIN}\",
        \"SecurityHeadersConfig\": {
          \"StrictTransportSecurity\": {
            \"Override\": true,
            \"AccessControlMaxAgeSec\": 31536000,
            \"IncludeSubdomains\": true,
            \"Preload\": true
          },
          \"ContentTypeOptions\": {\"Override\": true},
          \"FrameOptions\": {\"FrameOption\": \"DENY\", \"Override\": true},
          \"XSSProtection\": {\"Protection\": true, \"ModeBlock\": true, \"Override\": true},
          \"ReferrerPolicy\": {
            \"ReferrerPolicy\": \"strict-origin-when-cross-origin\",
            \"Override\": true
          }
        }
      }" \
      --query 'ResponseHeadersPolicy.Id' --output text)
    ok "Response headers policy: ${HEADERS_POLICY_ID}"
  else
    skip "Response headers policy: ${HEADERS_POLICY_ID}"
  fi

  # ── STEP 12: CloudFront distribution ──────────────────────────────────────
  echo "▶  12/15  CloudFront distribution"

  # Look up by CNAME (resilient to app renames) then fall back to Comment
  CF_DIST_ID=$(aws cloudfront list-distributions     --query "DistributionList.Items[?Aliases.Items[?@=='${APP_DOMAIN}']].Id"     --output text 2>/dev/null)
  if [ -z "${CF_DIST_ID}" ] || [ "${CF_DIST_ID}" = "None" ]; then
    CF_DIST_ID=$(aws cloudfront list-distributions       --query "DistributionList.Items[?Comment=='${CF_COMMENT}'].Id"       --output text 2>/dev/null)
  fi
  if [ -z "${CF_DIST_ID}" ] || [ "${CF_DIST_ID}" = "None" ]; then
    CF_DIST_ID=$(aws cloudfront list-distributions       --query "DistributionList.Items[?Comment=='publix-deal-checker'].Id"       --output text 2>/dev/null)
    [ -n "${CF_DIST_ID}" ] && [ "${CF_DIST_ID}" != "None" ] && echo "   → Found existing distribution by old name: ${CF_DIST_ID}"
  fi

  if [ -z "${CF_DIST_ID}" ] || [ "${CF_DIST_ID}" = "None" ]; then
    # Update S3 bucket policy to OAC-only BEFORE creating distribution
    # CloudFront needs the policy in place when it first contacts the origin
    aws s3api put-bucket-policy --bucket "${S3_FRONTEND}" \
      --policy "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
          \"Sid\": \"AllowCloudFrontOAC\",
          \"Effect\": \"Allow\",
          \"Principal\": {\"Service\": \"cloudfront.amazonaws.com\"},
          \"Action\": \"s3:GetObject\",
          \"Resource\": \"arn:aws:s3:::${S3_FRONTEND}/*\",
          \"Condition\": {
            \"StringEquals\": {
              \"AWS:SourceArn\": \"arn:aws:cloudfront::${AWS_ACCOUNT_ID}:distribution/${CF_DIST_ID}\"
            }
          }
        }]
      }" > /dev/null
    ok "S3 bucket policy updated (OAC-only)"

    # Block direct public access — CloudFront is the only entry point from here on
    aws s3api put-public-access-block --bucket "${S3_FRONTEND}" \
      --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" > /dev/null
    ok "S3 public access blocked (CloudFront-only)"

    CF_DIST_ID=$(aws cloudfront create-distribution \
      --distribution-config "{
        \"CallerReference\": \"${APP_NAME}-$(date +%s)\",
        \"Comment\": \"${CF_COMMENT}\",
        \"Enabled\": true,
        \"HttpVersion\": \"http2and3\",
        \"IsIPV6Enabled\": true,
        \"DefaultRootObject\": \"index.html\",
        \"Aliases\": {
          \"Quantity\": 2,
          \"Items\": [\"${APP_DOMAIN}\", \"www.${APP_DOMAIN}\"]
        },
        \"Origins\": {
          \"Quantity\": 1,
          \"Items\": [{
            \"Id\": \"s3-origin\",
            \"DomainName\": \"${S3_FRONTEND}.s3.${AWS_REGION}.amazonaws.com\",
            \"S3OriginConfig\": {\"OriginAccessIdentity\": \"\"},
            \"OriginAccessControlId\": \"${OAC_ID}\"
          }]
        },
        \"DefaultCacheBehavior\": {
          \"TargetOriginId\": \"s3-origin\",
          \"ViewerProtocolPolicy\": \"redirect-to-https\",
          \"CachePolicyId\": \"658327ea-f89d-4fab-a63d-7e88639e58f6\",
          \"OriginRequestPolicyId\": \"88a5eaf4-2fd4-4709-b370-b4c650ea3fcf\",
          \"ResponseHeadersPolicyId\": \"${HEADERS_POLICY_ID}\",
          \"Compress\": true,
          \"AllowedMethods\": {
            \"Quantity\": 2,
            \"Items\": [\"GET\", \"HEAD\"],
            \"CachedMethods\": {\"Quantity\": 2, \"Items\": [\"GET\", \"HEAD\"]}
          }
        },
        \"CustomErrorResponses\": {
          \"Quantity\": 1,
          \"Items\": [{
            \"ErrorCode\": 403,
            \"ResponsePagePath\": \"/index.html\",
            \"ResponseCode\": \"200\",
            \"ErrorCachingMinTTL\": 10
          }]
        },
        \"Logging\": {
          \"Enabled\": true,
          \"Bucket\": \"${CF_LOG_BUCKET}.s3.amazonaws.com\",
          \"Prefix\": \"${CF_LOG_PREFIX}\",
          \"IncludeCookies\": false
        },
        \"ViewerCertificate\": {
          \"ACMCertificateArn\": \"${ACM_CERT_ARN}\",
          \"SSLSupportMethod\": \"sni-only\",
          \"MinimumProtocolVersion\": \"TLSv1.2_2021\"
        },
        \"Restrictions\": {
          \"GeoRestriction\": {
            \"RestrictionType\": \"whitelist\",
            \"Quantity\": 1,
            \"Items\": [\"US\"]
          }
        }
      }" \
      --query 'Distribution.Id' --output text)
    ok "CloudFront distribution created: ${CF_DIST_ID}"
    info "Distribution takes ~15 minutes to deploy globally."
  else
    # Distribution exists — verify origin and logging bucket match current config.
    # This handles bucket renames (e.g. publix-deal-checker → cartwise).
    CURRENT_ORIGIN=$(aws cloudfront get-distribution \
      --id "${CF_DIST_ID}" \
      --query "Distribution.DistributionConfig.Origins.Items[0].DomainName" \
      --output text 2>/dev/null)
    EXPECTED_ORIGIN="${S3_FRONTEND}.s3.${AWS_REGION}.amazonaws.com"

    if [ "${CURRENT_ORIGIN}" != "${EXPECTED_ORIGIN}" ]; then
      info "CloudFront origin mismatch (${CURRENT_ORIGIN} → ${EXPECTED_ORIGIN}), updating..."
      CF_ETAG=$(aws cloudfront get-distribution-config \
        --id "${CF_DIST_ID}" --query "ETag" --output text)
      CF_TMP="$(cygpath -m "${SCRIPT_DIR}" 2>/dev/null || echo "${SCRIPT_DIR}")/cf_update_tmp.json"
      python3 -c "
import json, subprocess
result = subprocess.run(
    ['aws','cloudfront','get-distribution-config','--id','${CF_DIST_ID}','--output','json'],
    capture_output=True, text=True)
d = json.loads(result.stdout)
cfg = d['DistributionConfig']
cfg['Origins']['Items'][0]['DomainName'] = '${EXPECTED_ORIGIN}'
if cfg.get('Logging',{}).get('Bucket'):
    cfg['Logging']['Bucket'] = '${CF_LOG_BUCKET}.s3.amazonaws.com'
with open('${CF_TMP}', 'w') as f:
    json.dump(cfg, f)
print('Config written to ${CF_TMP}')
"
      MSYS_NO_PATHCONV=1 aws cloudfront update-distribution \
        --id "${CF_DIST_ID}" \
        --if-match "${CF_ETAG}" \
        --distribution-config "file://${CF_TMP}" \
        --query "Distribution.Status" --output text > /dev/null && \
        ok "CloudFront origin updated → ${EXPECTED_ORIGIN}" || \
        warn "CloudFront origin update failed — run manually if needed"
      rm -f "${CF_TMP}"

      # Apply OAC bucket policy to new bucket
      aws s3api put-bucket-policy --bucket "${S3_FRONTEND}" \
        --policy "{
          \"Version\": \"2012-10-17\",
          \"Statement\": [{
            \"Sid\": \"AllowCloudFrontOAC\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"cloudfront.amazonaws.com\"},
            \"Action\": \"s3:GetObject\",
            \"Resource\": \"arn:aws:s3:::${S3_FRONTEND}/*\",
            \"Condition\": {
              \"StringEquals\": {
                \"AWS:SourceArn\": \"arn:aws:cloudfront::${AWS_ACCOUNT_ID}:distribution/${CF_DIST_ID}\"
              }
            }
          }]
        }" > /dev/null && ok "S3 bucket policy updated for new origin"
      aws s3api put-public-access-block --bucket "${S3_FRONTEND}" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" > /dev/null
    else
      skip "CloudFront distribution: ${CF_DIST_ID}"
    fi
  fi

  # Fetch the CloudFront domain name for Route 53 (needed whether new or existing)
  CF_DOMAIN=$(aws cloudfront get-distribution \
    --id "${CF_DIST_ID}" \
    --query 'Distribution.DomainName' --output text 2>/dev/null)
  info "CloudFront domain: ${CF_DOMAIN}"

  # ── STEP 13: Route 53 DNS records ─────────────────────────────────────────
  echo "▶  13/15  Route 53 DNS records"

  # Z2FDTNDATAQYW2 is the fixed hosted zone ID for ALL CloudFront distributions
  CF_HOSTED_ZONE="Z2FDTNDATAQYW2"

  HOSTED_ZONE_ID=$(aws route53 list-hosted-zones-by-name \
    --dns-name "${APP_DOMAIN}" \
    --query 'HostedZones[0].Id' \
    --output text 2>/dev/null | sed 's|/hostedzone/||')

  if [ -z "${HOSTED_ZONE_ID}" ] || [ "${HOSTED_ZONE_ID}" = "None" ]; then
    echo "   ⚠️  No Route 53 hosted zone found for ${APP_DOMAIN} — DNS records skipped."
    echo "   Complete Phase 1 of the domain/SSL plan (register domain via Route 53) first."
  else
    info "Hosted zone: ${HOSTED_ZONE_ID}"

    # Upsert A alias records for apex and www → CloudFront
    aws route53 change-resource-record-sets \
      --hosted-zone-id "${HOSTED_ZONE_ID}" \
      --change-batch "{
        \"Changes\": [
          {
            \"Action\": \"UPSERT\",
            \"ResourceRecordSet\": {
              \"Name\": \"${APP_DOMAIN}\",
              \"Type\": \"A\",
              \"AliasTarget\": {
                \"HostedZoneId\": \"${CF_HOSTED_ZONE}\",
                \"DNSName\": \"${CF_DOMAIN}\",
                \"EvaluateTargetHealth\": false
              }
            }
          },
          {
            \"Action\": \"UPSERT\",
            \"ResourceRecordSet\": {
              \"Name\": \"www.${APP_DOMAIN}\",
              \"Type\": \"A\",
              \"AliasTarget\": {
                \"HostedZoneId\": \"${CF_HOSTED_ZONE}\",
                \"DNSName\": \"${CF_DOMAIN}\",
                \"EvaluateTargetHealth\": false
              }
            }
          }
        ]
      }" > /dev/null
    ok "Route 53: ${APP_DOMAIN} → ${CF_DOMAIN}"
    ok "Route 53: www.${APP_DOMAIN} → ${CF_DOMAIN}"

    # ── API CloudFront distribution (api.cartwise.shopping → API Gateway) ─────
    # Route API traffic through CloudFront so the Lambda receives
    # CloudFront-Viewer-Country / CloudFront-Viewer-City headers for geo-IP
    # logging. Also enforces US-only geo restriction on the API.
    #
    # Architecture:
    #   Browser → api.cartwise.shopping (Route 53 alias → CF distribution)
    #           → API GW execute-api URL (CustomOrigin, HTTPS, AllViewer policy)
    #           → Lambda
    #
    # The API GW custom domain (api.cartwise.shopping → API GW regional) is
    # kept as the CloudFront origin so the Host header resolves correctly and
    # the stage mapping is preserved. Route 53 is repointed from the API GW
    # regional domain to the new CF distribution.

    # ── 13a: API Gateway custom domain (origin for CF, if not already created) ─
    API_CUSTOM_DOMAIN_EXISTS=$(aws apigatewayv2 get-domain-name \
      --domain-name "${API_SUBDOMAIN}" \
      --query 'DomainName' --output text 2>/dev/null)

    if [ -z "${API_CUSTOM_DOMAIN_EXISTS}" ] || [ "${API_CUSTOM_DOMAIN_EXISTS}" = "None" ]; then
      aws apigatewayv2 create-domain-name \
        --domain-name "${API_SUBDOMAIN}" \
        --domain-name-configurations \
          "CertificateArn=${ACM_CERT_ARN},EndpointType=REGIONAL" > /dev/null
      ok "API Gateway custom domain: ${API_SUBDOMAIN}"
    else
      skip "API Gateway custom domain: ${API_SUBDOMAIN}"
    fi

    # Map the API Gateway stage to the custom domain (idempotent)
    aws apigatewayv2 create-api-mapping \
      --domain-name "${API_SUBDOMAIN}" \
      --api-id "${API_ID}" \
      --stage '$default' > /dev/null 2>&1 || true

    # The raw API GW execute-api URL is the CloudFront origin (not the custom domain).
    # CloudFront connects to it over HTTPS using the Host header for routing.
    API_GW_ORIGIN="${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"

    # ── 13b-i: Custom origin request policy (AllViewer + CF geo headers) ────
    # The managed AllViewer policy (b689b0a8) forwards all viewer-sent headers
    # but NOT CloudFront-injected headers like CloudFront-Viewer-Country/City.
    # We need a custom policy that adds those two headers so Lambda receives
    # them and can write geo-IP data to auth logs.
    GEO_ORP_NAME="${APP_NAME}-allviewer-geo"
    GEO_ORP_ID=$(aws cloudfront list-origin-request-policies \
      --type custom \
      --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='${GEO_ORP_NAME}'].OriginRequestPolicy.Id" \
      --output text 2>/dev/null)

    if [ -z "${GEO_ORP_ID}" ] || [ "${GEO_ORP_ID}" = "None" ]; then
      GEO_ORP_ID=$(aws cloudfront create-origin-request-policy \
        --origin-request-policy-config "{
          \"Name\": \"${GEO_ORP_NAME}\",
          \"Comment\": \"AllViewer plus CloudFront geo headers for ${APP_NAME}\",
          \"HeadersConfig\": {
            \"HeaderBehavior\": \"allViewerAndWhitelistCloudFront\",
            \"Headers\": {
              \"Quantity\": 2,
              \"Items\": [
                \"CloudFront-Viewer-Country\",
                \"CloudFront-Viewer-City\"
              ]
            }
          },
          \"CookiesConfig\": {\"CookieBehavior\": \"all\"},
          \"QueryStringsConfig\": {\"QueryStringBehavior\": \"all\"}
        }" \
        --query 'OriginRequestPolicy.Id' --output text)
      ok "Origin request policy (geo headers): ${GEO_ORP_ID}"
    else
      skip "Origin request policy (geo headers): ${GEO_ORP_ID}"
    fi

    # ── 13b: API CloudFront distribution ──────────────────────────────────────
    API_CF_DIST_ID=$(aws cloudfront list-distributions \
      --query "DistributionList.Items[?Aliases.Items[?@=='${API_SUBDOMAIN}']].Id" \
      --output text 2>/dev/null)

    if [ -z "${API_CF_DIST_ID}" ] || [ "${API_CF_DIST_ID}" = "None" ]; then
      API_CF_DIST_ID=$(aws cloudfront create-distribution \
        --distribution-config "{
          \"CallerReference\": \"${APP_NAME}-api-$(date +%s)\",
          \"Comment\": \"${CF_COMMENT}-api\",
          \"Enabled\": true,
          \"HttpVersion\": \"http2and3\",
          \"IsIPV6Enabled\": true,
          \"Aliases\": {
            \"Quantity\": 1,
            \"Items\": [\"${API_SUBDOMAIN}\"]
          },
          \"Origins\": {
            \"Quantity\": 1,
            \"Items\": [{
              \"Id\": \"api-origin\",
              \"DomainName\": \"${API_GW_ORIGIN}\",
              \"CustomOriginConfig\": {
                \"HTTPSPort\": 443,
                \"HTTPPort\": 80,
                \"OriginProtocolPolicy\": \"https-only\",
                \"OriginSslProtocols\": {
                  \"Quantity\": 1,
                  \"Items\": [\"TLSv1.2\"]
                }
              }
            }]
          },
          \"DefaultCacheBehavior\": {
            \"TargetOriginId\": \"api-origin\",
            \"ViewerProtocolPolicy\": \"https-only\",
            \"CachePolicyId\": \"4135ea2d-6df8-44a3-9df3-4b5a84be39ad\",
            \"OriginRequestPolicyId\": \"${GEO_ORP_ID}\",
            \"ResponseHeadersPolicyId\": \"${HEADERS_POLICY_ID}\",
            \"Compress\": true,
            \"AllowedMethods\": {
              \"Quantity\": 7,
              \"Items\": [\"GET\",\"HEAD\",\"OPTIONS\",\"PUT\",\"POST\",\"PATCH\",\"DELETE\"],
              \"CachedMethods\": {\"Quantity\": 2, \"Items\": [\"GET\",\"HEAD\"]}
            }
          },
          \"ViewerCertificate\": {
            \"ACMCertificateArn\": \"${ACM_CERT_ARN}\",
            \"SSLSupportMethod\": \"sni-only\",
            \"MinimumProtocolVersion\": \"TLSv1.2_2021\"
          },
          \"Restrictions\": {
            \"GeoRestriction\": {
              \"RestrictionType\": \"whitelist\",
              \"Quantity\": 1,
              \"Items\": [\"US\"]
            }
          }
        }" \
        --query 'Distribution.Id' --output text)
      ok "API CloudFront distribution created: ${API_CF_DIST_ID}"
      info "API distribution takes ~15 minutes to deploy globally."
    else
      skip "API CloudFront distribution: ${API_CF_DIST_ID}"
    fi

    # Fetch the CF domain name for Route 53
    API_CF_DOMAIN=$(aws cloudfront get-distribution \
      --id "${API_CF_DIST_ID}" \
      --query 'Distribution.DomainName' --output text 2>/dev/null)

    # ── 13c: Route 53 — point api. alias at CF distribution (not API GW directly) ─
    if [ -n "${API_CF_DOMAIN}" ] && [ "${API_CF_DOMAIN}" != "None" ]; then
      aws route53 change-resource-record-sets \
        --hosted-zone-id "${HOSTED_ZONE_ID}" \
        --change-batch "{
          \"Changes\": [{
            \"Action\": \"UPSERT\",
            \"ResourceRecordSet\": {
              \"Name\": \"${API_SUBDOMAIN}\",
              \"Type\": \"A\",
              \"AliasTarget\": {
                \"HostedZoneId\": \"Z2FDTNDATAQYW2\",
                \"DNSName\": \"${API_CF_DOMAIN}\",
                \"EvaluateTargetHealth\": false
              }
            }
          }]
        }" > /dev/null
      ok "Route 53: ${API_SUBDOMAIN} → ${API_CF_DOMAIN} (via CloudFront)"
    fi

    # Update FRONTEND_URL and API_URL to use the custom domain
    FRONTEND_URL="https://${APP_DOMAIN}"
    API_URL="https://${API_SUBDOMAIN}"
    info "URLs updated: FRONTEND_URL=${FRONTEND_URL}  API_URL=${API_URL}"
  fi

  # ── STEP 14: CloudFront cache invalidation ─────────────────────────────────
  echo "▶  14/15  CloudFront cache invalidation"

  if [ -n "${CF_DIST_ID}" ] && [ "${CF_DIST_ID}" != "None" ]; then
    INVAL_ID=$(aws cloudfront create-invalidation \
      --distribution-id "${CF_DIST_ID}" \
      --paths '/*' \
      --query 'Invalidation.Id' --output text 2>/dev/null)
    ok "Frontend cache invalidation queued: ${INVAL_ID}"
  else
    skip "Frontend cache invalidation (no distribution ID)"
  fi

  # Invalidate the API distribution too (picks up any Lambda/config changes)
  if [ -n "${API_CF_DIST_ID}" ] && [ "${API_CF_DIST_ID}" != "None" ]; then
    API_INVAL_ID=$(aws cloudfront create-invalidation \
      --distribution-id "${API_CF_DIST_ID}" \
      --paths '/*' \
      --query 'Invalidation.Id' --output text 2>/dev/null)
    ok "API cache invalidation queued: ${API_INVAL_ID}"
  fi

  # ── STEP 15: Update S3 website config ─────────────────────────────────────
  # Once CloudFront is in front, the S3 website endpoint is no longer needed.
  # Static website hosting can stay enabled (harmless) but direct HTTP access
  # is blocked by the public access block applied in Step 12.
  echo "▶  15/15  CloudFront domain active"
  ok "App served at: https://${APP_DOMAIN}"
  ok "API served at: https://${API_SUBDOMAIN}"

fi  # end CloudFront block

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║  ✅  Deployment complete!  (v9)                                   ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  Frontend:  ${FRONTEND_URL}"
echo "║  API:       ${API_URL}"
if [ -n "${CF_DIST_ID}" ] && [ "${CF_DIST_ID}" != "None" ]; then
echo "║  CF Frontend: ${CF_DIST_ID}"
fi
if [ -n "${API_CF_DIST_ID}" ] && [ "${API_CF_DIST_ID}" != "None" ]; then
echo "║  CF API:      ${API_CF_DIST_ID}"
fi
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  Admin secret (keep this safe!):                                  ║"
printf '║  %s\n' "${ADMIN_SECRET}"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  Test scraper now:                                                ║"
echo "║    aws lambda invoke --function-name ${LAMBDA_SCRAPER} \\"
echo "║      --region ${AWS_REGION} /tmp/out.json && cat /tmp/out.json"
echo "║  Tail logs:                                                       ║"
echo "║    aws logs tail /aws/lambda/${LAMBDA_SCRAPER} --follow"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
