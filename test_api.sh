#!/usr/bin/env bash
# ─── Laya Decision API — curl test suite ────────────────────────────────────
# Usage:
#   chmod +x test_api.sh
#   ./test_api.sh
#
# Overrides:
#   LAYA_BASE_URL=http://myserver:8000 LAYA_API_KEY=my-key ./test_api.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BASE_URL="${LAYA_BASE_URL:-http://localhost:8000}"
API_KEY="${LAYA_API_KEY:-}"

PASS="\033[32mPASS\033[0m"
FAIL="\033[31mFAIL\033[0m"
PASSED=0
FAILED=0

# ── helpers ───────────────────────────────────────────────────────────────────
check() {
  local name="$1" expected_status="$2" actual_status="$3" body="$4"
  if [ "$actual_status" -eq "$expected_status" ]; then
    printf "[${PASS}] %s  (HTTP %s)\n" "$name" "$actual_status"
    PASSED=$((PASSED + 1))
  else
    printf "[${FAIL}] %s  (expected HTTP %s, got %s)\n" "$name" "$expected_status" "$actual_status"
    echo "       $body"
    FAILED=$((FAILED + 1))
  fi
}

auth_header() {
  if [ -n "$API_KEY" ]; then
    echo "-H \"Authorization: Bearer ${API_KEY}\""
  fi
}

# ── shared fixture ────────────────────────────────────────────────────────────
BILLING_STATE='{"subject":"Duplicate charge","body":"Billed twice for March invoice #4411. Refund today or we cancel."}'
TECH_STATE='"The dashboard keeps throwing a 500 error since the last deployment."'
SALES_STATE='"I would like to upgrade our plan to include 20 more seats."'

QUESTIONS='{
  "team": {
    "type": "choice",
    "instructions": "Which team should handle this request?",
    "criteria": {
      "billing":   "invoices, payments, refunds",
      "technical": "bugs, outages, errors",
      "sales":     "pricing, new plans",
      "other":     "everything else"
    }
  },
  "churn": {
    "type": "noul",
    "instructions": "Does the customer explicitly threaten to cancel?"
  },
  "urgency": {
    "type": "score",
    "instructions": "How urgent is this request?",
    "criteria": ["not urgent", "soon", "today"]
  }
}'

printf "\n%s\n" "══════════════════════════════════════════════════════════════"
printf "  Laya API test  →  %s\n" "$BASE_URL"
printf "  auth=%s\n" "${API_KEY:+yes (key set)}"
printf "%s\n\n" "══════════════════════════════════════════════════════════════"

# ── 1. health (no auth) ───────────────────────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  "${BASE_URL}/health")
BODY=$(cat /tmp/laya_body.json)
check "GET /health (no auth)" 200 "$RESP" "$BODY"
echo "       $(echo "$BODY" | grep -o '"gpu_available":[^,}]*\|"device":[^,}]*\|"rocm_hip":[^,}]*' | tr '\n' '  ')"

# ── 2. list models (no auth) ──────────────────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  "${BASE_URL}/v1/models")
BODY=$(cat /tmp/laya_body.json)
check "GET /v1/models (no auth)" 200 "$RESP" "$BODY"
echo "       $(echo "$BODY" | grep -o '"id":"[^"]*"' | tr '\n' '  ')"

# ── 3. auth rejection ─────────────────────────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  -X POST "${BASE_URL}/v1/systemone" \
  -H "Content-Type: application/json" \
  -d '{"state":"test","questions":{}}')
BODY=$(cat /tmp/laya_body.json)
check "POST /v1/systemone — no token → 401" 401 "$RESP" "$BODY"

# ── 4. single decision — billing ticket ───────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  -X POST "${BASE_URL}/v1/systemone" \
  -H "Content-Type: application/json" \
  ${API_KEY:+-H "Authorization: Bearer ${API_KEY}"} \
  -d "{\"model\":\"laya-multilingual\",\"state\":${BILLING_STATE},\"questions\":${QUESTIONS}}")
BODY=$(cat /tmp/laya_body.json)
check "POST /v1/systemone — billing ticket" 200 "$RESP" "$BODY"
echo "       $(echo "$BODY" | grep -o '"choice":"[^"]*"\|"noul":[0-9.]*\|"score":[0-9.]*' | head -3 | tr '\n' '  ')"

# ── 5. single decision — technical ticket (no model field) ────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  -X POST "${BASE_URL}/v1/systemone" \
  -H "Content-Type: application/json" \
  ${API_KEY:+-H "Authorization: Bearer ${API_KEY}"} \
  -d "{\"state\":${TECH_STATE},\"questions\":${QUESTIONS}}")
BODY=$(cat /tmp/laya_body.json)
check "POST /v1/systemone — technical ticket (no model field)" 200 "$RESP" "$BODY"
echo "       $(echo "$BODY" | grep -o '"choice":"[^"]*"\|"noul":[0-9.]*\|"score":[0-9.]*' | head -3 | tr '\n' '  ')"

# ── 6. model alias jev-latest ─────────────────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  -X POST "${BASE_URL}/v1/systemone" \
  -H "Content-Type: application/json" \
  ${API_KEY:+-H "Authorization: Bearer ${API_KEY}"} \
  -d '{"model":"jev-latest","state":"Quick question about pricing.","questions":{"team":{"type":"choice","instructions":"Which team?","criteria":{"billing":"payments","technical":"bugs","sales":"pricing","other":"else"}}}}')
BODY=$(cat /tmp/laya_body.json)
check "POST /v1/systemone — model alias jev-latest" 200 "$RESP" "$BODY"
echo "       resolved model=$(echo "$BODY" | grep -o '"model":"[^"]*"' | head -1)"

# ── 7. batch — 3 items ────────────────────────────────────────────────────────
RESP=$(curl -s -o /tmp/laya_body.json -w "%{http_code}" \
  -X POST "${BASE_URL}/v1/systemone/batch" \
  -H "Content-Type: application/json" \
  ${API_KEY:+-H "Authorization: Bearer ${API_KEY}"} \
  -d "{
    \"model\": \"laya-multilingual\",
    \"items\": [${BILLING_STATE}, ${TECH_STATE}, ${SALES_STATE}],
    \"questions\": {
      \"team\": {
        \"type\": \"choice\",
        \"instructions\": \"Which team owns this?\",
        \"criteria\": {\"billing\":\"payments\",\"technical\":\"bugs\",\"sales\":\"plans\",\"other\":\"else\"}
      }
    }
  }")
BODY=$(cat /tmp/laya_body.json)
check "POST /v1/systemone/batch — 3 items" 200 "$RESP" "$BODY"
echo "       teams=$(echo "$BODY" | grep -o '"choice":"[^"]*"' | tr '\n' '  ')"

# ── summary ───────────────────────────────────────────────────────────────────
TOTAL=$((PASSED + FAILED))
printf "\n%s\n" "══════════════════════════════════════════════════════════════"
if [ "$FAILED" -eq 0 ]; then
  printf "  %s/%s passed 🎉\n" "$PASSED" "$TOTAL"
else
  printf "  %s/%s passed — %s failed\n" "$PASSED" "$TOTAL" "$FAILED"
fi
printf "%s\n\n" "══════════════════════════════════════════════════════════════"

exit "$FAILED"
