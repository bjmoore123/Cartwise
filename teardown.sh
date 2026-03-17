#!/bin/bash
# =============================================================================
#  teardown.sh — Cartwise v9
#  Removes all AWS resources created by deploy.sh.
#
#  By default, DynamoDB tables (and all user data) are PRESERVED.
#  Pass --delete-data to also drop all DynamoDB tables.
#
#  Usage:
#    bash teardown.sh                  # keeps DynamoDB data
#    bash teardown.sh --delete-data    # deletes DynamoDB data too
# =============================================================================
export MSYS_NO_PATHCONV=1
AWS_REGION="us-east-1"
APP_NAME="cartwise"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
S3_FRONTEND="${APP_NAME}-frontend-${AWS_ACCOUNT_ID}"
CF_LOG_BUCKET="${APP_NAME}-cf-logs"
APP_DOMAIN="${APP_DOMAIN:-cartwise.shopping}"
API_SUBDOMAIN="api.${APP_DOMAIN}"
CF_COMMENT="${APP_NAME}"

DELETE_DATA=false
for arg in "$@"; do
  [ "$arg" = "--delete-data" ] && DELETE_DATA=true
done

ok()   { echo "   ✓ Deleted: $*"; }
skip() { echo "   - Not found: $*"; }
keep() { echo "   ✦ Preserved: $*"; }
info() { echo "   → $*"; }

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Cartwise v9 — Teardown                  ║"
if [ "$DELETE_DATA" = "true" ]; then
echo "║   ⚠️  --delete-data: DynamoDB tables WILL be deleted  ║"
else
echo "║   DynamoDB data will be PRESERVED (use --delete-data) ║"
fi
echo "╚══════════════════════════════════════════════════════╝"
echo ""
if [ "$DELETE_DATA" = "true" ]; then
  read -p "Delete ALL resources INCLUDING all user/deal data? (yes/no): " C
else
  read -p "Delete all AWS resources except DynamoDB data? (yes/no): " C
fi
[ "${C}" != "yes" ] && echo "Aborted." && exit 0

# ── CloudFront distribution ───────────────────────────────────────────────────
# Must be disabled before it can be deleted. Disabling takes ~5-10 minutes.
echo "▶  CloudFront distribution"
CF_DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${CF_COMMENT}'].Id" \
  --output text 2>/dev/null)

if [ -n "${CF_DIST_ID}" ] && [ "${CF_DIST_ID}" != "None" ]; then
  CF_ENABLED=$(aws cloudfront get-distribution-config \
    --id "${CF_DIST_ID}" \
    --query 'DistributionConfig.Enabled' --output text 2>/dev/null)

  if [ "${CF_ENABLED}" = "true" ]; then
    info "Disabling CloudFront distribution ${CF_DIST_ID}..."
    CF_ETAG=$(aws cloudfront get-distribution-config \
      --id "${CF_DIST_ID}" --query 'ETag' --output text)
    aws cloudfront get-distribution-config \
      --id "${CF_DIST_ID}" \
      --query 'DistributionConfig' \
      --output json 2>/dev/null \
      | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
cfg['Enabled'] = False
print(json.dumps(cfg))
" > /tmp/cf_config_disabled.json
    aws cloudfront update-distribution \
      --id "${CF_DIST_ID}" \
      --if-match "${CF_ETAG}" \
      --distribution-config "file:///tmp/cf_config_disabled.json" > /dev/null
    info "Waiting for distribution to disable (~5-10 min)..."
    aws cloudfront wait distribution-deployed --id "${CF_DIST_ID}" 2>/dev/null || sleep 360
    ok "CloudFront distribution disabled"
  fi

  CF_ETAG=$(aws cloudfront get-distribution-config \
    --id "${CF_DIST_ID}" --query 'ETag' --output text 2>/dev/null)
  aws cloudfront delete-distribution \
    --id "${CF_DIST_ID}" --if-match "${CF_ETAG}" > /dev/null 2>&1 \
    && ok "CloudFront distribution: ${CF_DIST_ID}" \
    || echo "   ⚠️  Delete failed — re-run teardown once distribution reaches Deployed state."
else
  skip "CloudFront distribution"
fi

# ── CloudFront Origin Access Control ─────────────────────────────────────────
echo "▶  CloudFront OAC"
OAC_NAME="${APP_NAME}-oac"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id" \
  --output text 2>/dev/null)
if [ -n "${OAC_ID}" ] && [ "${OAC_ID}" != "None" ]; then
  OAC_ETAG=$(aws cloudfront get-origin-access-control \
    --id "${OAC_ID}" --query 'ETag' --output text 2>/dev/null)
  aws cloudfront delete-origin-access-control \
    --id "${OAC_ID}" --if-match "${OAC_ETAG}" > /dev/null 2>&1 \
    && ok "CloudFront OAC: ${OAC_ID}" \
    || skip "CloudFront OAC: ${OAC_ID}"
else
  skip "CloudFront OAC"
fi

# ── CloudFront response headers policy ───────────────────────────────────────
echo "▶  CloudFront response headers policy"
HEADERS_POLICY_NAME="${APP_NAME}-security-headers"
HEADERS_POLICY_ID=$(aws cloudfront list-response-headers-policies \
  --type custom \
  --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='${HEADERS_POLICY_NAME}'].ResponseHeadersPolicy.Id" \
  --output text 2>/dev/null)
if [ -n "${HEADERS_POLICY_ID}" ] && [ "${HEADERS_POLICY_ID}" != "None" ]; then
  HP_ETAG=$(aws cloudfront get-response-headers-policy \
    --id "${HEADERS_POLICY_ID}" --query 'ETag' --output text 2>/dev/null)
  aws cloudfront delete-response-headers-policy \
    --id "${HEADERS_POLICY_ID}" --if-match "${HP_ETAG}" > /dev/null 2>&1 \
    && ok "Response headers policy: ${HEADERS_POLICY_ID}" \
    || skip "Response headers policy: ${HEADERS_POLICY_ID}"
else
  skip "Response headers policy"
fi

# ── Route 53 DNS records ──────────────────────────────────────────────────────
echo "▶  Route 53 DNS records"
HOSTED_ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "${APP_DOMAIN}" \
  --query 'HostedZones[0].Id' \
  --output text 2>/dev/null | sed 's|/hostedzone/||')

if [ -n "${HOSTED_ZONE_ID}" ] && [ "${HOSTED_ZONE_ID}" != "None" ]; then
  for RECORD_NAME in "${APP_DOMAIN}" "www.${APP_DOMAIN}" "${API_SUBDOMAIN}"; do
    RECORD_JSON=$(aws route53 list-resource-record-sets \
      --hosted-zone-id "${HOSTED_ZONE_ID}" \
      --query "ResourceRecordSets[?Name=='${RECORD_NAME}.' && Type=='A'] | [0]" \
      --output json 2>/dev/null)
    if [ "${RECORD_JSON}" != "null" ] && [ -n "${RECORD_JSON}" ]; then
      echo "${RECORD_JSON}" | python3 -c "
import json, sys
rec = json.load(sys.stdin)
batch = {'Changes': [{'Action': 'DELETE', 'ResourceRecordSet': rec}]}
print(json.dumps(batch))
" > /tmp/r53_del.json
      aws route53 change-resource-record-sets \
        --hosted-zone-id "${HOSTED_ZONE_ID}" \
        --change-batch "file:///tmp/r53_del.json" > /dev/null 2>&1 \
        && ok "Route 53 A record: ${RECORD_NAME}" \
        || echo "   ⚠️  Could not delete Route 53 A record: ${RECORD_NAME}"
    else
      skip "Route 53 A record: ${RECORD_NAME}"
    fi
  done
  info "ACM validation CNAME preserved (required for certificate auto-renewal)"
else
  skip "Route 53 hosted zone for ${APP_DOMAIN}"
fi

# ── API Gateway custom domain ─────────────────────────────────────────────────
echo "▶  API Gateway custom domain"
API_DOMAIN_EXISTS=$(aws apigatewayv2 get-domain-name \
  --domain-name "${API_SUBDOMAIN}" \
  --query 'DomainName' --output text 2>/dev/null)
if [ -n "${API_DOMAIN_EXISTS}" ] && [ "${API_DOMAIN_EXISTS}" != "None" ]; then
  MAPPING_ID=$(aws apigatewayv2 get-api-mappings \
    --domain-name "${API_SUBDOMAIN}" \
    --query 'Items[0].ApiMappingId' --output text 2>/dev/null)
  [ -n "${MAPPING_ID}" ] && [ "${MAPPING_ID}" != "None" ] && \
    aws apigatewayv2 delete-api-mapping \
      --domain-name "${API_SUBDOMAIN}" \
      --api-mapping-id "${MAPPING_ID}" > /dev/null 2>&1 || true
  aws apigatewayv2 delete-domain-name \
    --domain-name "${API_SUBDOMAIN}" > /dev/null 2>&1 \
    && ok "API Gateway custom domain: ${API_SUBDOMAIN}" \
    || skip "API Gateway custom domain: ${API_SUBDOMAIN}"
else
  skip "API Gateway custom domain: ${API_SUBDOMAIN}"
fi

# ── CloudWatch alarm ──────────────────────────────────────────────────────────
echo "▶  CloudWatch cert-expiry alarm"
aws cloudwatch delete-alarms \
  --alarm-names "pdc-cert-expiry-warning" \
  --region "${AWS_REGION}" > /dev/null 2>&1 \
  && ok "CloudWatch alarm: pdc-cert-expiry-warning" \
  || skip "CloudWatch alarm: pdc-cert-expiry-warning"

# ── SNS topic ─────────────────────────────────────────────────────────────────
echo "▶  SNS cert-expiry topic"
SNS_ARN=$(aws sns list-topics \
  --query "Topics[?ends_with(TopicArn,'pdc-cert-expiry-alert')].TopicArn" \
  --output text 2>/dev/null)
if [ -n "${SNS_ARN}" ] && [ "${SNS_ARN}" != "None" ]; then
  aws sns delete-topic --topic-arn "${SNS_ARN}" > /dev/null 2>&1 \
    && ok "SNS topic: pdc-cert-expiry-alert" \
    || skip "SNS topic: pdc-cert-expiry-alert"
else
  skip "SNS topic: pdc-cert-expiry-alert"
fi

# ── EventBridge ───────────────────────────────────────────────────────────────
echo "▶  EventBridge"
RULE="${APP_NAME}-weekly"
aws events remove-targets --rule "${RULE}" --ids scraper --region "${AWS_REGION}" > /dev/null 2>&1 && ok "EventBridge targets" || skip "EventBridge targets"
aws events delete-rule    --name "${RULE}" --region "${AWS_REGION}" > /dev/null 2>&1 && ok "EventBridge rule" || skip "EventBridge rule"

# ── API Gateway ───────────────────────────────────────────────────────────────
echo "▶  API Gateway"
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" --query "Items[?Name=='${APP_NAME}-api-gw'].ApiId" --output text 2>/dev/null)
[ -n "${API_ID}" ] && [ "${API_ID}" != "None" ] && \
  aws apigatewayv2 delete-api --api-id "${API_ID}" --region "${AWS_REGION}" > /dev/null && \
  ok "API Gateway: ${API_ID}" || skip "API Gateway"

# ── Lambda functions ──────────────────────────────────────────────────────────
echo "▶  Lambda functions"
for fn in "${APP_NAME}-api" "${APP_NAME}-scraper"; do
  aws lambda delete-function --function-name "${fn}" --region "${AWS_REGION}" > /dev/null 2>&1 && ok "Lambda: ${fn}" || skip "Lambda: ${fn}"
done

# ── IAM role ──────────────────────────────────────────────────────────────────
echo "▶  IAM role"
ROLE="${APP_NAME}-lambda-role"
aws iam detach-role-policy --role-name "${ROLE}" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole > /dev/null 2>&1 || true
aws iam delete-role-policy --role-name "${ROLE}" --policy-name "${APP_NAME}-lambda-policy" > /dev/null 2>&1 || true
aws iam delete-role        --role-name "${ROLE}" > /dev/null 2>&1 && ok "IAM role" || skip "IAM role"

# ── DynamoDB tables ───────────────────────────────────────────────────────────
echo "▶  DynamoDB tables"
ALL_TABLES=(
  "${APP_NAME}-users"
  "${APP_NAME}-sessions"
  "${APP_NAME}-admin-sessions"
  "${APP_NAME}-deals"
  "${APP_NAME}-scrape-logs"
  "${APP_NAME}-auth-logs"
  "${APP_NAME}-app-logs"
  "${APP_NAME}-deal-history"
  "${APP_NAME}-deal-corpus"
)

if [ "$DELETE_DATA" = "true" ]; then
  for tbl in "${ALL_TABLES[@]}"; do
    aws dynamodb delete-table --table-name "${tbl}" --region "${AWS_REGION}" > /dev/null 2>&1 && ok "DynamoDB: ${tbl}" || skip "DynamoDB: ${tbl}"
  done
else
  for tbl in "${ALL_TABLES[@]}"; do
    keep "DynamoDB: ${tbl}"
  done
  echo "   → Re-run with --delete-data to remove all DynamoDB tables and user data."
fi

# ── SSM parameters ────────────────────────────────────────────────────────────
echo "▶  SSM parameters"
for p in resend-api-key resend-from-name resend-from-addr; do
  aws ssm delete-parameter --name "/publix/${p}" --region "${AWS_REGION}" > /dev/null 2>&1 && ok "SSM: /publix/${p}" || skip "SSM: /publix/${p}"
done

# ── S3 CloudFront log bucket ──────────────────────────────────────────────────
echo "▶  S3 CloudFront log bucket"
if aws s3api head-bucket --bucket "${CF_LOG_BUCKET}" > /dev/null 2>&1; then
  aws s3 rm "s3://${CF_LOG_BUCKET}" --recursive > /dev/null
  aws s3api delete-bucket --bucket "${CF_LOG_BUCKET}" --region "${AWS_REGION}" > /dev/null
  ok "S3: ${CF_LOG_BUCKET}"
else
  skip "S3: ${CF_LOG_BUCKET}"
fi

# ── S3 frontend ───────────────────────────────────────────────────────────────
echo "▶  S3 frontend"
if aws s3api head-bucket --bucket "${S3_FRONTEND}" > /dev/null 2>&1; then
  # Restore public access block before emptying in case bucket was locked to OAC-only
  aws s3api put-public-access-block --bucket "${S3_FRONTEND}" \
    --public-access-block-configuration \
      "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false" > /dev/null 2>&1 || true
  aws s3 rm "s3://${S3_FRONTEND}" --recursive > /dev/null
  aws s3api delete-bucket --bucket "${S3_FRONTEND}" --region "${AWS_REGION}" > /dev/null
  ok "S3: ${S3_FRONTEND}"
else
  skip "S3: ${S3_FRONTEND}"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
if [ "$DELETE_DATA" = "true" ]; then
echo "║  ✅  Teardown complete (all data deleted).                       ║"
else
echo "║  ✅  Teardown complete (DynamoDB data preserved).                ║"
fi
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  NOT deleted (manual action required if desired):               ║"
echo "║    • ACM certificate:                                           ║"
echo "║      aws acm delete-certificate --region us-east-1 \\            ║"
echo "║        --certificate-arn YOUR_CERT_ARN                          ║"
echo "║    • Route 53 hosted zone (contains ACM validation CNAME)       ║"
echo "║    • Route 53 domain registration                               ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
