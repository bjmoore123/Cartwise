#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# ONE-TIME TEARDOWN: publix-deal-checker → cartwise migration
#
# Removes all AWS resources created under the old "publix-deal-checker" name.
# Run AFTER deploying the new "cartwise" stack and verifying it is working.
#
# ⚠️  THIS IS DESTRUCTIVE AND IRREVERSIBLE.
#     DynamoDB tables (users, sessions, deals, history, corpus, logs) will be
#     permanently deleted. Export any data you want to keep first.
#
# Usage:
#   bash teardown-publix-deal-checker.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

OLD="publix-deal-checker"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

ok()   { echo "   ✓ $*"; }
skip() { echo "   - $* (not found / already removed)"; }
warn() { echo "   ⚠ $*"; }
exists() { "$@" > /dev/null 2>&1; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  publix-deal-checker → cartwise  ONE-TIME TEARDOWN   ║"
echo "║  Account: ${AWS_ACCOUNT_ID}   Region: ${AWS_REGION}  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  ⚠️  This will permanently delete all publix-deal-checker AWS resources."
echo "  ⚠️  Data in DynamoDB tables will be LOST."
echo ""
read -r -p "  Type 'delete publix-deal-checker' to confirm: " CONFIRM
if [ "${CONFIRM}" != "delete publix-deal-checker" ]; then
  echo "Aborted."
  exit 1
fi
echo ""

# ── DynamoDB tables ────────────────────────────────────────────────────────────
echo "▶  DynamoDB tables"
for tbl in \
  "${OLD}-users" \
  "${OLD}-sessions" \
  "${OLD}-admin-sessions" \
  "${OLD}-deals" \
  "${OLD}-scrape-logs" \
  "${OLD}-auth-logs" \
  "${OLD}-app-logs" \
  "${OLD}-deal-history" \
  "${OLD}-deal-corpus"
do
  if exists aws dynamodb describe-table --table-name "${tbl}" --region "${AWS_REGION}"; then
    aws dynamodb delete-table --table-name "${tbl}" --region "${AWS_REGION}" > /dev/null
    ok "Deleted DynamoDB table: ${tbl}"
  else
    skip "DynamoDB table: ${tbl}"
  fi
done

# ── Lambda functions ────────────────────────────────────────────────────────────
echo "▶  Lambda functions"
for fn in "${OLD}-api" "${OLD}-scraper"; do
  if exists aws lambda get-function --function-name "${fn}" --region "${AWS_REGION}"; then
    aws lambda delete-function --function-name "${fn}" --region "${AWS_REGION}"
    ok "Deleted Lambda: ${fn}"
  else
    skip "Lambda: ${fn}"
  fi
done

# ── API Gateway ─────────────────────────────────────────────────────────────────
echo "▶  API Gateway"
# The API Gateway (tdlo5l10dh) is SHARED with the new cartwise stack —
# it was renamed in-place (APP_NAME changed, resource names updated) but the
# API ID, custom domain mapping, and stage are all reused. Do NOT delete it.
warn "Skipping API Gateway deletion — reused by new cartwise stack (same API ID, custom domain)"

# ── API Gateway custom domain ───────────────────────────────────────────────────
# NOTE: api.cartwise.shopping is KEPT (used by new stack). Only remove if it
# pointed exclusively to the old API GW. Check manually before uncommenting:
# aws apigatewayv2 delete-domain-name --domain-name api.cartwise.shopping
warn "Skipping api.cartwise.shopping domain deletion — shared with new stack"

# ── EventBridge rule ─────────────────────────────────────────────────────────────
echo "▶  EventBridge"
OLD_RULE="${OLD}-weekly"
if exists aws events describe-rule --name "${OLD_RULE}" --region "${AWS_REGION}"; then
  # Remove targets first
  TARGET_IDS=$(aws events list-targets-by-rule --rule "${OLD_RULE}" --region "${AWS_REGION}" \
    --query "Targets[*].Id" --output text 2>/dev/null || true)
  if [ -n "${TARGET_IDS}" ]; then
    MSYS_NO_PATHCONV=1 aws events remove-targets --rule "${OLD_RULE}" --region "${AWS_REGION}" \
      --ids ${TARGET_IDS} > /dev/null 2>&1 || true
  fi
  aws events delete-rule --name "${OLD_RULE}" --region "${AWS_REGION}"
  ok "Deleted EventBridge rule: ${OLD_RULE}"
else
  skip "EventBridge rule: ${OLD_RULE}"
fi

# ── S3 frontend bucket ──────────────────────────────────────────────────────────
echo "▶  S3 frontend bucket"
OLD_BUCKET="${OLD}-frontend-${AWS_ACCOUNT_ID}"
if exists aws s3api head-bucket --bucket "${OLD_BUCKET}"; then
  echo "   Emptying bucket ${OLD_BUCKET}..."
  aws s3 rm "s3://${OLD_BUCKET}" --recursive --quiet
  aws s3api delete-bucket --bucket "${OLD_BUCKET}" --region "${AWS_REGION}"
  ok "Deleted S3 bucket: ${OLD_BUCKET}"
else
  skip "S3 bucket: ${OLD_BUCKET}"
fi

# ── S3 CloudFront log bucket ────────────────────────────────────────────────────
echo "▶  S3 CF log bucket"
OLD_LOG_BUCKET="${OLD}-cf-logs"
if exists aws s3api head-bucket --bucket "${OLD_LOG_BUCKET}"; then
  echo "   Emptying log bucket ${OLD_LOG_BUCKET}..."
  aws s3 rm "s3://${OLD_LOG_BUCKET}" --recursive --quiet
  aws s3api delete-bucket --bucket "${OLD_LOG_BUCKET}" --region "${AWS_REGION}"
  ok "Deleted S3 log bucket: ${OLD_LOG_BUCKET}"
else
  skip "S3 log bucket: ${OLD_LOG_BUCKET}"
fi

# ── IAM role and policy ─────────────────────────────────────────────────────────
echo "▶  IAM"
OLD_ROLE="${OLD}-lambda-role"
OLD_POLICY="${OLD}-lambda-policy"
if exists aws iam get-role --role-name "${OLD_ROLE}"; then
  # Detach all managed policies first
  ATTACHED=$(aws iam list-attached-role-policies --role-name "${OLD_ROLE}" \
    --query "AttachedPolicies[*].PolicyArn" --output text 2>/dev/null || true)
  for arn in $ATTACHED; do
    aws iam detach-role-policy --role-name "${OLD_ROLE}" --policy-arn "${arn}" 2>/dev/null || true
  done
  # Delete inline policies
  INLINE=$(aws iam list-role-policies --role-name "${OLD_ROLE}" \
    --query "PolicyNames" --output text 2>/dev/null || true)
  for pname in $INLINE; do
    aws iam delete-role-policy --role-name "${OLD_ROLE}" --policy-name "${pname}" 2>/dev/null || true
  done
  aws iam delete-role --role-name "${OLD_ROLE}"
  ok "Deleted IAM role: ${OLD_ROLE}"
else
  skip "IAM role: ${OLD_ROLE}"
fi

# Delete standalone policy if it exists
OLD_POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${OLD_POLICY}"
if exists aws iam get-policy --policy-arn "${OLD_POLICY_ARN}"; then
  # Delete all non-default versions first
  VERSIONS=$(aws iam list-policy-versions --policy-arn "${OLD_POLICY_ARN}" \
    --query "Versions[?!IsDefaultVersion].VersionId" --output text 2>/dev/null || true)
  for v in $VERSIONS; do
    aws iam delete-policy-version --policy-arn "${OLD_POLICY_ARN}" --version-id "${v}" 2>/dev/null || true
  done
  aws iam delete-policy --policy-arn "${OLD_POLICY_ARN}"
  ok "Deleted IAM policy: ${OLD_POLICY}"
else
  skip "IAM policy: ${OLD_POLICY}"
fi

# ── SSM parameters ──────────────────────────────────────────────────────────────
echo "▶  SSM parameters"
# NOTE: /publix/* SSM parameters are kept — they may still be referenced or useful.
# Delete them manually if desired:
# aws ssm delete-parameter --name /publix/resend-api-key
warn "Skipping /publix/* SSM parameters — delete manually if no longer needed"

# ── CloudWatch log groups ───────────────────────────────────────────────────────
echo "▶  CloudWatch log groups"
for lg in \
  "/aws/lambda/${OLD}-api" \
  "/aws/lambda/${OLD}-scraper"
do
  if exists aws logs describe-log-groups --log-group-name-prefix "${lg}" \
    --query "logGroups[0].logGroupName" --output text --region "${AWS_REGION}"; then
    aws logs delete-log-group --log-group-name "${lg}" --region "${AWS_REGION}" 2>/dev/null && \
      ok "Deleted log group: ${lg}" || skip "Log group: ${lg}"
  else
    skip "Log group: ${lg}"
  fi
done

# ── CloudFront distribution ──────────────────────────────────────────────────────
# The CloudFront distribution E17SNZABDYYAVE points to cartwise.shopping and is
# SHARED with / reused by the new cartwise stack. Do NOT delete it.
warn "CloudFront distribution E17SNZABDYYAVE retained — used by new cartwise stack"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  publix-deal-checker teardown complete               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Remaining manual steps if desired:"
echo "  • Delete SSM params: aws ssm delete-parameter --name /publix/resend-api-key"
echo "  • CloudFront OAC / response headers policy (if not reused by cartwise)"
echo "  • Route 53 records already point to new stack — no action needed"
echo ""
