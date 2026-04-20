#!/usr/bin/env bash
# Pings every service's health endpoint and reports pass/fail.
# Usage: bash scripts/verify-stack.sh
# Run after: docker compose up -d && sleep 30

set -euo pipefail

PASS=0
FAIL=0
ERRORS=()

# ── colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

ok()   { printf "  ${GREEN}✓${RESET}  %-35s ${GREEN}OK${RESET}\n"   "$1"; PASS=$((PASS+1)); }
fail() { printf "  ${RED}✗${RESET}  %-35s ${RED}FAIL${RESET}  %s\n" "$1" "$2"; FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); }
info() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

# ── check helper ──────────────────────────────────────────────────────────────
# check <label> <url> <grep_pattern>
check() {
  local label="$1" url="$2" pattern="${3:-}"
  local body
  body=$(curl -sf --max-time 5 "$url" 2>/dev/null) || { fail "$label" "no response from $url"; return; }
  if [[ -n "$pattern" ]] && ! echo "$body" | grep -qE "$pattern"; then
    fail "$label" "response did not match '$pattern'"
  else
    ok "$label"
  fi
}

# ── docker-based check for containers without HTTP health ─────────────────────
check_docker() {
  local label="$1" container="$2"
  local state
  state=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null) || \
    state=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null) || \
    state="not found"
  if [[ "$state" == "healthy" ]]; then
    ok "$label (docker)"
  else
    fail "$label (docker)" "container state: $state"
  fi
}

# ═════════════════════════════════════════════════════════════════════════════
printf "\n${BOLD}Network Observability Platform — Stack Verification${RESET}\n"
printf "$(date '+%Y-%m-%d %H:%M:%S')\n"

# ── Redpanda ──────────────────────────────────────────────────────────────────
info "Redpanda"
check "Broker admin API"   "http://localhost:9644/v1/status/ready"  "\"status\":\"ready\""
check "Schema Registry"    "http://localhost:8081/subjects"          ""
check "Redpanda Console"   "http://localhost:8080/"                  ""

# Verify topics via rpk inside the broker container (the admin HTTP API does not list topics)
info "Kafka Topics"
TOPIC_LIST=$(docker exec netobserv-redpanda rpk topic list --brokers localhost:9092 2>/dev/null) || TOPIC_LIST=""
for topic in network.flows network.bgp network.snmp anomalies.scored anomalies.runbooks; do
  if echo "$TOPIC_LIST" | grep -q "^$topic"; then
    ok "topic: $topic"
  else
    fail "topic: $topic" "not found — run: docker compose up redpanda-init"
  fi
done

# ── InfluxDB ──────────────────────────────────────────────────────────────────
info "InfluxDB"
check "InfluxDB health"    "http://localhost:8086/health"            "\"status\":\"pass\""

# ── OpenSearch ────────────────────────────────────────────────────────────────
info "OpenSearch"
check "Cluster health"     "http://localhost:9200/_cluster/health"   "\"status\":\"(green|yellow)\""
check "OpenSearch Dashboards" "http://localhost:5601/api/status"     "\"overall\""

# ── DynamoDB Local ────────────────────────────────────────────────────────────
info "DynamoDB Local"
check_docker "DynamoDB Local" "netobserv-dynamodb"

# ── MinIO ─────────────────────────────────────────────────────────────────────
info "MinIO"
check "MinIO liveness"     "http://localhost:9000/minio/health/live"  ""
check "MinIO console"      "http://localhost:9001"                    ""

# ── Postgres ──────────────────────────────────────────────────────────────────
info "Postgres + pgvector"
if docker exec netobserv-postgres pg_isready -q 2>/dev/null; then
  ok "Postgres pg_isready"
else
  fail "Postgres pg_isready" "container not ready — check: docker compose logs postgres"
fi

# Verify pgvector extension is installed
PG_CHECK=$(docker exec netobserv-postgres psql -U netobserv -d netobserv -tAc \
  "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null) || PG_CHECK=""
if [[ "$PG_CHECK" == "vector" ]]; then
  ok "pgvector extension"
else
  fail "pgvector extension" "extension 'vector' not found in netobserv database"
fi

# ── Grafana ───────────────────────────────────────────────────────────────────
info "Grafana"
check "Grafana health"     "http://localhost:3001/api/health"         "\"database\""

# ═════════════════════════════════════════════════════════════════════════════
printf "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "  ${GREEN}Passed:${RESET} $PASS   ${RED}Failed:${RESET} $FAIL\n"
printf "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"

if [[ $FAIL -gt 0 ]]; then
  printf "${RED}Failed checks:${RESET}\n"
  for e in "${ERRORS[@]}"; do printf "  • %s\n" "$e"; done
  printf "\n${YELLOW}Tip:${RESET} services may still be starting — wait 30s and re-run.\n\n"
  exit 1
fi

printf "${GREEN}All services healthy. Stack is ready.${RESET}\n\n"
