#!/bin/bash
# =============================================================================
#  teardown.sh — Cartwise v9.1
#  Removes ALL AWS resources created by deploy.sh.
#
#  By default, DynamoDB tables (and all user data) are PRESERVED.
#
#  Usage:
#    bash teardown.sh                  # keeps DynamoDB data
#    bash teardown.sh --delete-data    # deletes DynamoDB data too
#    bash teardown.sh --dry-run        # preview what would be deleted
#    bash teardown.sh --delete-data --dry-run
# =============================================================================
export MSYS_NO_PATHCONV=1

# ── Configuration ─────────────────────────────────────────────────────────────

AWS_REGION="us-east-1"
APP_NAME="cartwise"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "${AWS_ACCOUNT_ID}" ]; then
  echo "❌ Could not get AWS account ID. Check your AWS CLI credentials."
  exit 1
fi

S3_FRONTEND="${APP_NAME}-frontend-${AWS_ACCOUNT_ID}"
CF_LOG_BUCKET="${APP_NAME}-cf-logs"
APP_DOMAIN="${APP_DOMAIN:-cartwise.shopping}"
API_SUBDOMAIN="api.${APP_DOMAIN}"
CF_COMMENT="${APP_NAME}"

# ── Parse arguments ──────────────────────────────────────────────────────────

DELETE_DATA=false
DRY_RUN=false
for arg in "$@"; do
  [ "$arg" = "--delete-data" ] && DELETE_DATA=true
  [ "$arg" = "--dry-run" ]     && DRY_RUN=true
done

# ── Counters ─────────────────────────────────────────────────────────────────

DELETED=0
SKIPPED=0
PRESERVED=0
FAILED=0
STEP=0
TOTAL_STEPS=15

ok()   { DELETED=$((DELETED+1));   echo "   ✓ Deleted: $*"; }
skip() { SKIPPED=$((SKIPPED+1));   echo "   - Not found: $*"; }
keep() { PRESERVED=$((PRESERVED+1)); echo "   ✦ Preserved: $*"; }
fail() { FAILED=$((FAILED+1));    echo "   ⚠️  Failed: $*"; }
info() { echo "   → $*"; }
step() { STEP=$((STEP+1)); echo ""; echo "▶  ${STEP}/${TOTAL_STEPS}  $*"; }

dry() {
  if [ "${DRY_RUN}" = "true" ]; then
    echo "   🔍 Would delete: $*"
    return 0
  fi
  return 1
}

# ── Banner ───────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   Cartwise v9.1 — Teardown                                     ║"
if [ "${DRY_RUN}" = "true" ]; then
echo "║   🔍 DRY RUN — no resources will be modified                    ║"
elif [ "$DELETE_DATA" = "true" ]; then
echo "║   ⚠️  --delete-data: DynamoDB tables WILL be deleted             ║"
else
echo "║   DynamoDB data will be PRESERVED (use --delete-data to remove) ║"
fi
echo "║                                                                 ║"
echo "║   Account:  ${AWS_ACCOUNT_ID}                                 ║"
echo "║   Region:   ${AWS_REGION}                                         ║"
echo "║   Domain:   ${APP_DOMAIN}                                  ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

if [ "${DRY_RUN}" != "true" ]; then
  if [ "$DELETE_DATA" = "true" ]; then
    read -p "⚠️  Delete ALL resources INCLUDING all user/deal data? (yes/no): " C
  else
    read -p "Delete all AWS resources except DynamoDB data? (yes/no): " C
  fi
  [ "${C}" != "yes" ] && echo "Aborted." && exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# TEARDOWN — ordered by dependency (consumers before providers)
# ═════════════════════════════════════════════════════════════════════════════

# ── 1. CloudFront: Frontend distribution ─────────────────────────────────────
# Must be disabled before deletion. Disabling takes ~5-10 minutes.
step "CloudFront: frontend distribution"

CF_DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${CF_COMMENT}'].Id" \
  --output text 2>/dev/null)

if [ -n "${CF_DIST_ID}" ] && [ "${CF_DIST_ID}" != "None" ]; then
  if dry "CloudFront frontend distribution: ${CF_DIST_ID}"; then :
  else
    CF_ENABLED=$(aws cloudfront get-distribution-config \
      --id "${CF_DIST_ID}" \
      --query 'DistributionConfig.Enabled' --output text 2>/dev/null)

    if [ "${CF_ENABLED}" = "true" ]; then
      info "Disabling distribution ${CF_DIST_ID} (takes ~5-10 min)..."
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
        --distribution-config "file:///tmp/cf_config_disabled.json" > /dev/null 2>&1
      info "Waiting for distribution to reach Deployed state..."
      aws cloudfront wait distribution-deployed --id "${CF_DIST_ID}" 2>/dev/null || sleep 360
    fi

    CF_ETAG=$(aws cloudfront get-distribution-config \
      --id "${CF_DIST_ID}" --query 'ETag' --output text 2>/dev/null)
    aws cloudfront delete-distribution \
      --id "${CF_DIST_ID}" --if-match "${CF_ETAG}" > /dev/null 2>&1 \
      && ok "CloudFront frontend distribution: ${CF_DIST_ID}" \
      || fail "CloudFront frontend distribution — re-run once it reaches Deployed state"
  fi
else
  skip "CloudFront frontend distribution"
fi

# ── 2. CloudFront: API distribution ──────────────────────────────────────────
step "CloudFront: API distribution"

API_CF_DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Aliases.Items[?@=='${API_SUBDOMAIN}']].Id" \
  --output text 2>/dev/null)

if [ -n "${API_CF_DIST_ID}" ] && [ "${API_CF_DIST_ID}" != "None" ]; then
  if dry "CloudFront API distribution: ${API_CF_DIST_ID}"; then :
  else
    API_CF_ENABLED=$(aws cloudfront get-distribution-config \
      --id "${API_CF_DIST_ID}" \
      --query 'DistributionConfig.Enabled' --output text 2>/dev/null)

    if [ "${API_CF_ENABLED}" = "true" ]; then
      info "Disabling API distribution ${API_CF_DIST_ID} (takes ~5-10 min)..."
      API_CF_ETAG=$(aws cloudfront get-distribution-config \
        --id "${API_CF_DIST_ID}" --query 'ETag' --output text)
      aws cloudfront get-distribution-config \
        --id "${API_CF_DIST_ID}" \
        --query 'DistributionConfig' \
        --output json 2>/dev/null \
        | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
cfg['Enabled'] = False
print(json.dumps(cfg))
" > /tmp/api_cf_config_disabled.json
      aws cloudfront update-distribution \
        --id "${API_CF_DIST_ID}" \
        --if-match "${API_CF_ETAG}" \
        --distribution-config "file:///tmp/api_cf_config_disabled.json" > /dev/null 2>&1
      info "Waiting for API distribution to reach Deployed state..."
      aws cloudfront wait distribution-deployed --id "${API_CF_DIST_ID}" 2>/dev/null || sleep 360
    fi

    API_CF_ETAG=$(aws cloudfront get-distribution-config \
      --id "${API_CF_DIST_ID}" --query 'ETag' --output text 2>/dev/null)
    aws cloudfront delete-distribution \
      --id "${API_CF_DIST_ID}" --if-match "${API_CF_ETAG}" > /dev/null 2>&1 \
      && ok "CloudFront API distribution: ${API_CF_DIST_ID}" \
      || fail "CloudFront API distribution — re-run once it reaches Deployed state"
  fi
else
  skip "CloudFront API distribution"
fi

# ── 3. CloudFront: OAC, response headers policy, origin request policy ───────
step "CloudFront: policies & OAC"

# OAC
OAC_NAME="${APP_NAME}-oac"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id" \
  --output text 2>/dev/null)
if [ -n "${OAC_ID}" ] && [ "${OAC_ID}" != "None" ]; then
  if dry "CloudFront OAC: ${OAC_NAME}"; then :
  else
    OAC_ETAG=$(aws cloudfront get-origin-access-control \
      --id "${OAC_ID}" --query 'ETag' --output text 2>/dev/null)
    aws cloudfront delete-origin-access-control \
      --id "${OAC_ID}" --if-match "${OAC_ETAG}" > /dev/null 2>&1 \
      && ok "CloudFront OAC: ${OAC_NAME}" \
      || fail "CloudFront OAC: ${OAC_NAME}"
  fi
else
  skip "CloudFront OAC"
fi

# Response headers policy
HEADERS_POLICY_NAME="${APP_NAME}-security-headers"
HEADERS_POLICY_ID=$(aws cloudfront list-response-headers-policies \
  --type custom \
  --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='${HEADERS_POLICY_NAME}'].ResponseHeadersPolicy.Id" \
  --output text 2>/dev/null)
if [ -n "${HEADERS_POLICY_ID}" ] && [ "${HEADERS_POLICY_ID}" != "None" ]; then
  if dry "Response headers policy: ${HEADERS_POLICY_NAME}"; then :
  else
    HP_ETAG=$(aws cloudfront get-response-headers-policy \
      --id "${HEADERS_POLICY_ID}" --query 'ETag' --output text 2>/dev/null)
    aws cloudfront delete-response-headers-policy \
      --id "${HEADERS_POLICY_ID}" --if-match "${HP_ETAG}" > /dev/null 2>&1 \
      && ok "Response headers policy: ${HEADERS_POLICY_NAME}" \
      || fail "Response headers policy: ${HEADERS_POLICY_NAME}"
  fi
else
  skip "Response headers policy"
fi

# Origin request policy (geo headers)
GEO_ORP_NAME="${APP_NAME}-allviewer-geo"
GEO_ORP_ID=$(aws cloudfront list-origin-request-policies \
  --type custom \
  --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='${GEO_ORP_NAME}'].OriginRequestPolicy.Id" \
  --output text 2>/dev/null)
if [ -n "${GEO_ORP_ID}" ] && [ "${GEO_ORP_ID}" != "None" ]; then
  if dry "Origin request policy: ${GEO_ORP_NAME}"; then :
  else
    ORP_ETAG=$(aws cloudfront get-origin-request-policy \
      --id "${GEO_ORP_ID}" --query 'ETag' --output text 2>/dev/null)
    aws cloudfront delete-origin-request-policy \
      --id "${GEO_ORP_ID}" --if-match "${ORP_ETAG}" > /dev/null 2>&1 \
      && ok "Origin request policy: ${GEO_ORP_NAME}" \
      || fail "Origin request policy: ${GEO_ORP_NAME}"
  fi
else
  skip "Origin request policy (geo headers)"
fi

# ── 4. Route 53 DNS records ──────────────────────────────────────────────────
step "Route 53 DNS records"

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
      if dry "Route 53 A record: ${RECORD_NAME}"; then continue; fi
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
        || fail "Route 53 A record: ${RECORD_NAME}"
    else
      skip "Route 53 A record: ${RECORD_NAME}"
    fi
  done
  info "ACM validation CNAME preserved (required for certificate auto-renewal)"
else
  skip "Route 53 hosted zone for ${APP_DOMAIN}"
fi

# ── 5. API Gateway: custom domain + mapping ──────────────────────────────────
step "API Gateway: custom domain"

API_DOMAIN_EXISTS=$(aws apigatewayv2 get-domain-name \
  --domain-name "${API_SUBDOMAIN}" \
  --query 'DomainName' --output text 2>/dev/null)
if [ -n "${API_DOMAIN_EXISTS}" ] && [ "${API_DOMAIN_EXISTS}" != "None" ]; then
  if dry "API Gateway custom domain: ${API_SUBDOMAIN}"; then :
  else
    # Delete mapping first, then domain
    MAPPING_ID=$(aws apigatewayv2 get-api-mappings \
      --domain-name "${API_SUBDOMAIN}" \
      --query 'Items[0].ApiMappingId' --output text 2>/dev/null)
    if [ -n "${MAPPING_ID}" ] && [ "${MAPPING_ID}" != "None" ]; then
      aws apigatewayv2 delete-api-mapping \
        --domain-name "${API_SUBDOMAIN}" \
        --api-mapping-id "${MAPPING_ID}" > /dev/null 2>&1 || true
    fi
    aws apigatewayv2 delete-domain-name \
      --domain-name "${API_SUBDOMAIN}" > /dev/null 2>&1 \
      && ok "API Gateway custom domain: ${API_SUBDOMAIN}" \
      || fail "API Gateway custom domain: ${API_SUBDOMAIN}"
  fi
else
  skip "API Gateway custom domain"
fi

# ── 6. API Gateway: HTTP API ─────────────────────────────────────────────────
step "API Gateway: HTTP API"

API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${APP_NAME}-api-gw'].ApiId" \
  --output text 2>/dev/null)
if [ -n "${API_ID}" ] && [ "${API_ID}" != "None" ]; then
  if dry "API Gateway: ${APP_NAME}-api-gw (${API_ID})"; then :
  else
    aws apigatewayv2 delete-api --api-id "${API_ID}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "API Gateway: ${APP_NAME}-api-gw (${API_ID})" \
      || fail "API Gateway: ${APP_NAME}-api-gw"
  fi
else
  skip "API Gateway"
fi

# ── 7. EventBridge: weekly + monthly-cleanup rules ───────────────────────────
step "EventBridge rules"

for RULE_NAME in "${APP_NAME}-weekly" "${APP_NAME}-monthly-cleanup"; do
  # Check if rule exists
  RULE_STATE=$(aws events describe-rule --name "${RULE_NAME}" --region "${AWS_REGION}" \
    --query 'State' --output text 2>/dev/null)
  if [ -n "${RULE_STATE}" ] && [ "${RULE_STATE}" != "None" ]; then
    if dry "EventBridge rule: ${RULE_NAME}"; then continue; fi
    # List and remove all targets first
    TARGET_IDS=$(aws events list-targets-by-rule --rule "${RULE_NAME}" --region "${AWS_REGION}" \
      --query 'Targets[].Id' --output text 2>/dev/null)
    if [ -n "${TARGET_IDS}" ] && [ "${TARGET_IDS}" != "None" ]; then
      aws events remove-targets --rule "${RULE_NAME}" \
        --ids ${TARGET_IDS} --region "${AWS_REGION}" > /dev/null 2>&1 || true
    fi
    aws events delete-rule --name "${RULE_NAME}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "EventBridge rule: ${RULE_NAME}" \
      || fail "EventBridge rule: ${RULE_NAME}"
  else
    skip "EventBridge rule: ${RULE_NAME}"
  fi
done

# ── 8. Lambda functions ──────────────────────────────────────────────────────
step "Lambda functions"

for fn in "${APP_NAME}-api" "${APP_NAME}-scraper"; do
  EXISTS=$(aws lambda get-function --function-name "${fn}" --region "${AWS_REGION}" \
    --query 'Configuration.FunctionName' --output text 2>/dev/null)
  if [ -n "${EXISTS}" ] && [ "${EXISTS}" != "None" ]; then
    if dry "Lambda function: ${fn}"; then continue; fi
    aws lambda delete-function --function-name "${fn}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "Lambda: ${fn}" \
      || fail "Lambda: ${fn}"
  else
    skip "Lambda: ${fn}"
  fi
done

# ── 9. IAM role + policies ───────────────────────────────────────────────────
step "IAM role"

ROLE="${APP_NAME}-lambda-role"
ROLE_EXISTS=$(aws iam get-role --role-name "${ROLE}" --query 'Role.RoleName' --output text 2>/dev/null)
if [ -n "${ROLE_EXISTS}" ] && [ "${ROLE_EXISTS}" != "None" ]; then
  if dry "IAM role: ${ROLE}"; then :
  else
    # Detach all managed policies
    ATTACHED=$(aws iam list-attached-role-policies --role-name "${ROLE}" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null)
    for arn in ${ATTACHED}; do
      aws iam detach-role-policy --role-name "${ROLE}" --policy-arn "${arn}" > /dev/null 2>&1 || true
    done
    # Delete all inline policies
    INLINE=$(aws iam list-role-policies --role-name "${ROLE}" \
      --query 'PolicyNames[]' --output text 2>/dev/null)
    for pol in ${INLINE}; do
      aws iam delete-role-policy --role-name "${ROLE}" --policy-name "${pol}" > /dev/null 2>&1 || true
    done
    aws iam delete-role --role-name "${ROLE}" > /dev/null 2>&1 \
      && ok "IAM role: ${ROLE}" \
      || fail "IAM role: ${ROLE}"
  fi
else
  skip "IAM role: ${ROLE}"
fi

# ── 10. DynamoDB tables ──────────────────────────────────────────────────────
step "DynamoDB tables"

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
    EXISTS=$(aws dynamodb describe-table --table-name "${tbl}" --region "${AWS_REGION}" \
      --query 'Table.TableName' --output text 2>/dev/null)
    if [ -n "${EXISTS}" ] && [ "${EXISTS}" != "None" ]; then
      if dry "DynamoDB table: ${tbl}"; then continue; fi
      aws dynamodb delete-table --table-name "${tbl}" --region "${AWS_REGION}" > /dev/null 2>&1 \
        && ok "DynamoDB: ${tbl}" \
        || fail "DynamoDB: ${tbl}"
    else
      skip "DynamoDB: ${tbl}"
    fi
  done
else
  for tbl in "${ALL_TABLES[@]}"; do
    keep "DynamoDB: ${tbl}"
  done
  info "Re-run with --delete-data to remove all DynamoDB tables and user data."
fi

# ── 11. SSM parameters ──────────────────────────────────────────────────────
step "SSM parameters"

SSM_PARAMS=(
  "/publix/resend-api-key"
  "/publix/resend-from-name"
  "/publix/resend-from-addr"
)

for p in "${SSM_PARAMS[@]}"; do
  EXISTS=$(aws ssm get-parameter --name "${p}" --region "${AWS_REGION}" \
    --query 'Parameter.Name' --output text 2>/dev/null)
  if [ -n "${EXISTS}" ] && [ "${EXISTS}" != "None" ]; then
    if dry "SSM parameter: ${p}"; then continue; fi
    aws ssm delete-parameter --name "${p}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "SSM: ${p}" \
      || fail "SSM: ${p}"
  else
    skip "SSM: ${p}"
  fi
done

# ── 12. CloudWatch log groups ────────────────────────────────────────────────
step "CloudWatch log groups"

for lg in "/aws/lambda/${APP_NAME}-api" "/aws/lambda/${APP_NAME}-scraper"; do
  EXISTS=$(aws logs describe-log-groups --log-group-name-prefix "${lg}" --region "${AWS_REGION}" \
    --query "logGroups[?logGroupName=='${lg}'].logGroupName" --output text 2>/dev/null)
  if [ -n "${EXISTS}" ] && [ "${EXISTS}" != "None" ]; then
    if dry "CloudWatch log group: ${lg}"; then continue; fi
    aws logs delete-log-group --log-group-name "${lg}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "CloudWatch log group: ${lg}" \
      || fail "CloudWatch log group: ${lg}"
  else
    skip "CloudWatch log group: ${lg}"
  fi
done

# ── 13. CloudWatch alarms + SNS topics ───────────────────────────────────────
step "CloudWatch alarms & SNS topics"

# Check both legacy (pdc-) and current (cartwise-) naming conventions
for ALARM_NAME in "pdc-cert-expiry-warning" "${APP_NAME}-cert-expiry-warning"; do
  EXISTS=$(aws cloudwatch describe-alarms --alarm-names "${ALARM_NAME}" --region "${AWS_REGION}" \
    --query 'MetricAlarms[0].AlarmName' --output text 2>/dev/null)
  if [ -n "${EXISTS}" ] && [ "${EXISTS}" != "None" ]; then
    if dry "CloudWatch alarm: ${ALARM_NAME}"; then continue; fi
    aws cloudwatch delete-alarms --alarm-names "${ALARM_NAME}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "CloudWatch alarm: ${ALARM_NAME}" \
      || fail "CloudWatch alarm: ${ALARM_NAME}"
  else
    skip "CloudWatch alarm: ${ALARM_NAME}"
  fi
done

for TOPIC_SUFFIX in "pdc-cert-expiry-alert" "${APP_NAME}-cert-expiry-alert"; do
  SNS_ARN=$(aws sns list-topics \
    --query "Topics[?ends_with(TopicArn,'${TOPIC_SUFFIX}')].TopicArn" \
    --output text 2>/dev/null)
  if [ -n "${SNS_ARN}" ] && [ "${SNS_ARN}" != "None" ]; then
    if dry "SNS topic: ${TOPIC_SUFFIX}"; then continue; fi
    aws sns delete-topic --topic-arn "${SNS_ARN}" > /dev/null 2>&1 \
      && ok "SNS topic: ${TOPIC_SUFFIX}" \
      || fail "SNS topic: ${TOPIC_SUFFIX}"
  else
    skip "SNS topic: ${TOPIC_SUFFIX}"
  fi
done

# ── 14. S3 buckets ───────────────────────────────────────────────────────────
step "S3 buckets"

# CloudFront log bucket
if aws s3api head-bucket --bucket "${CF_LOG_BUCKET}" > /dev/null 2>&1; then
  if dry "S3 bucket: ${CF_LOG_BUCKET}"; then :
  else
    aws s3 rm "s3://${CF_LOG_BUCKET}" --recursive --quiet > /dev/null 2>&1
    aws s3api delete-bucket --bucket "${CF_LOG_BUCKET}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "S3: ${CF_LOG_BUCKET}" \
      || fail "S3: ${CF_LOG_BUCKET}"
  fi
else
  skip "S3: ${CF_LOG_BUCKET}"
fi

# Frontend bucket
if aws s3api head-bucket --bucket "${S3_FRONTEND}" > /dev/null 2>&1; then
  if dry "S3 bucket: ${S3_FRONTEND}"; then :
  else
    # Remove public access block + bucket policy so we can empty and delete
    aws s3api put-public-access-block --bucket "${S3_FRONTEND}" \
      --public-access-block-configuration \
        "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false" \
        > /dev/null 2>&1 || true
    aws s3api delete-bucket-policy --bucket "${S3_FRONTEND}" > /dev/null 2>&1 || true
    aws s3 rm "s3://${S3_FRONTEND}" --recursive --quiet > /dev/null 2>&1
    aws s3api delete-bucket --bucket "${S3_FRONTEND}" --region "${AWS_REGION}" > /dev/null 2>&1 \
      && ok "S3: ${S3_FRONTEND}" \
      || fail "S3: ${S3_FRONTEND}"
  fi
else
  skip "S3: ${S3_FRONTEND}"
fi

# ── 15. Cleanup temp files ───────────────────────────────────────────────────
step "Cleanup"

rm -f /tmp/cf_config_disabled.json /tmp/api_cf_config_disabled.json /tmp/r53_del.json 2>/dev/null
info "Temp files cleaned"

# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
if [ "${DRY_RUN}" = "true" ]; then
echo "║  🔍 DRY RUN COMPLETE — no resources were modified               ║"
elif [ "$DELETE_DATA" = "true" ]; then
echo "║  ✅  Teardown complete (all data deleted)                        ║"
else
echo "║  ✅  Teardown complete (DynamoDB data preserved)                 ║"
fi
echo "╠══════════════════════════════════════════════════════════════════╣"
printf "║  Deleted: %-4s  Skipped: %-4s  Preserved: %-4s  Failed: %-4s  ║\n" \
  "${DELETED}" "${SKIPPED}" "${PRESERVED}" "${FAILED}"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  NOT deleted (manual action if desired):                        ║"
echo "║    • ACM certificate:                                           ║"
echo "║        aws acm delete-certificate --region us-east-1 \\          ║"
echo "║          --certificate-arn <YOUR_CERT_ARN>                      ║"
echo "║    • Route 53 hosted zone (contains ACM validation CNAME)       ║"
echo "║    • Route 53 domain registration                               ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

if [ "${FAILED}" -gt 0 ]; then
  echo "⚠️  ${FAILED} resource(s) failed to delete. Re-run teardown to retry."
  echo "   CloudFront distributions may need a few minutes to reach Deployed state."
  echo ""
fi
