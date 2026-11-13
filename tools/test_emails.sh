#!/usr/bin/env bash
# tools/test_emails.sh — send one of every Cartwise email on STAGING to your inbox (v9.11).
#
#   bash tools/test_emails.sh                         # weekly alert goes to moore.ben@gmail.com
#   bash tools/test_emails.sh you@example.com         # weekly alert goes to that staging account
#
# Makes a throwaway account with a Gmail "+" address (moore.ben+cwHHMM@gmail.com), which Gmail
# delivers to your normal inbox, and walks it through every email:
#   1 verification   2 verification reminder   3 PIN reset   4 PIN changed (you click the link)
#   5 test email     6 weekly deal alert (your real staging account)   7 unsubscribe check
# Staging only: every name and URL below is the staging one. Needs: curl, aws, python.
#
# Limits if you run it more than once: 5 sign-ups an hour per IP, 3 reset emails an hour per
# address, 1 test email per ~10 minutes. Each run uses a new + address, so runs don't collide.
# Clean up afterwards in staging Admin -> Users (search "moore.ben+cw").

set -uo pipefail

API="https://api-staging.cartwise.shopping"
USERS_TABLE="cartwise-staging-users"
SCRAPER="cartwise-staging-scraper"
INBOX_USER="moore.ben"
INBOX_DOMAIN="gmail.com"
WEEKLY_TO="${1:-${INBOX_USER}@${INBOX_DOMAIN}}"
NEW="${INBOX_USER}+cw$(date +%H%M)@${INBOX_DOMAIN}"
PIN1="7391"
# Relative file in the current folder: Windows aws.exe can't open Git Bash /tmp/... paths
OUT="cw_test_out.json"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '   \033[31m✗\033[0m %s\n' "$*"; }
pause(){ read -r -p "   Press Enter when you've got it… " _; }
json() { python -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }
post() { curl -s -X POST "$API$1" -H 'Content-Type: application/json' "${@:2}"; }
invoke() {   # invoke the staging scraper with a JSON payload, print its body
  rm -f "$OUT"
  if aws lambda invoke --function-name "$SCRAPER" --cli-binary-format raw-in-base64-out \
       --payload "$1" "$OUT" >/dev/null && [ -f "$OUT" ]; then
    cat "$OUT"
  else
    printf '\033[31m✗\033[0m scraper call failed (see the AWS error above)'
  fi
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
bold "Cartwise email test (staging)"
echo "   Test account:  $NEW"
echo "   Weekly alert:  $WEEKLY_TO"
for tool in curl aws python; do
  command -v "$tool" >/dev/null || { bad "$tool not found"; exit 1; }
done
aws lambda get-function --function-name "$SCRAPER" --query 'Configuration.FunctionName' --output text >/dev/null 2>&1 \
  || { bad "Can't see $SCRAPER (AWS credentials / region?)"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$API/auth/forgot-pin" \
       -H 'Content-Type: application/json' -d '{"email":"not-an-email"}')
[ "$code" != "000" ] || { bad "Staging API not answering at $API"; exit 1; }
ok "staging API and scraper reachable"

# ── 1. Verification email ────────────────────────────────────────────────────
bold "1/7  Verification email (create the test account)"
r=$(post /auth/register -d "{\"email\":\"$NEW\",\"pin\":\"$PIN1\"}")
echo "   $r"
echo "   Look for: \"Verify your Cartwise email address\". Don't click Verify yet."
pause

# ── 2. Verification reminder ─────────────────────────────────────────────────
bold "2/7  Verification reminder (account made to look 8 days old)"
EIGHT_DAYS_AGO=$(date -u -d '8 days ago' +%Y-%m-%dT%H:%M:%S+00:00)
aws dynamodb update-item --table-name "$USERS_TABLE" \
  --key "{\"email\":{\"S\":\"$NEW\"}}" --update-expression "SET created_at = :c" \
  --condition-expression "attribute_exists(email)" \
  --expression-attribute-values "{\":c\":{\"S\":\"$EIGHT_DAYS_AGO\"}}" \
  && ok "created_at set to $EIGHT_DAYS_AGO" || { bad "test account not found; did step 1 work?"; exit 1; }
echo "   $(invoke '{"source":"verify-reminders"}')"
echo "   Expect \"sent\": 1 or more (other due unverified staging accounts get one too)."
echo "   Look for: \"Reminder: verify your Cartwise email\". Don't click anything yet."
pause

# ── 3. PIN reset ──────────────────────────────────────────────────────────────
bold "3/7  PIN reset email"
echo "   $(post /auth/forgot-pin -d "{\"email\":\"$NEW\"}" | json message)"
echo "   Look for: \"Reset your Cartwise PIN\"."
pause

# ── 4. PIN changed ────────────────────────────────────────────────────────────
bold "4/7  \"Your PIN was changed\" email"
echo "   In the reset email, click \"Choose a new PIN\" and set a new PIN (this also verifies"
echo "   the test account). The \"Your Cartwise PIN was changed\" email follows."
read -r -s -p "   Type the new PIN you chose (hidden), then Enter: " PIN2; echo
[[ "$PIN2" =~ ^[0-9]{4}$ ]] || { bad "PIN must be 4 digits"; exit 1; }

# ── 5. Test email ─────────────────────────────────────────────────────────────
bold "5/7  Test email"
TOKEN=""
for try in 1 2 3; do
  resp=$(post /auth/login -d "{\"email\":\"$NEW\",\"pin\":\"$PIN2\"}")
  TOKEN=$(printf '%s' "$resp" | json token)
  [ -n "$TOKEN" ] && break
  bad "sign-in failed: $(printf '%s' "$resp" | json error)"
  [ "$try" -lt 3 ] || break
  read -r -s -p "   Re-type the PIN you set in step 4 (hidden), or just Enter to skip: " PIN2; echo
  [ -n "$PIN2" ] || break
done
if [ -n "$TOKEN" ]; then
  ok "signed in as the test account"
  echo "   $(curl -s -X POST "$API/user/test-email" -H "Authorization: Bearer $TOKEN" | python -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('message') or d.get('error'))")"
  echo "   Look for: \"Cartwise test email\"."
  curl -s -X POST "$API/auth/logout" -H "Authorization: Bearer $TOKEN" >/dev/null
else
  bad "skipping the test email (finish step 4 first: the reset link must be used)"
fi

# ── 6. Weekly deal alert ──────────────────────────────────────────────────────
bold "6/7  Weekly deal alert → $WEEKLY_TO"
echo "   $(invoke "{\"source\":\"manual-resend\",\"target_email\":\"$WEEKLY_TO\"}")"
echo "   Arrives in a minute or two if that staging account has a store and something on its"
echo "   list is on sale. Nothing on sale (or no such account) = no email."
pause

# ── 7. Unsubscribe ────────────────────────────────────────────────────────────
bold "7/7  Unsubscribe"
echo "   Open the reminder from step 2 (or the verification email from step 1), scroll to the"
echo "   bottom, click \"Unsubscribe from Cartwise emails\" → \"Yes, unsubscribe me\"."
pause
STATE=$(aws dynamodb get-item --table-name "$USERS_TABLE" --key "{\"email\":{\"S\":\"$NEW\"}}" \
        --query 'Item.prefs.M.email_enabled.BOOL' --output text 2>/dev/null)
[ "$STATE" = "False" ] && ok "test account is unsubscribed (email_enabled = false)" \
                       || bad "test account still subscribed (email_enabled = $STATE)"

bold "Done. Check each email on your computer and your phone:"
cat <<'EOF'
   • logo shows in the green header (Gmail: click "Display images" once)
   • buttons are on one line; nothing scrolls sideways on the phone
   • your address is never split mid-word (only at the @, if at all)
   • every email ends with "Unsubscribe from Cartwise emails"
EOF
echo "   Clean up: staging Admin → Users → delete $NEW"
rm -f "$OUT" 2>/dev/null
