#!/usr/bin/env bash
# Verification ladder for the local stack: one check per hop, in the order data
# flows. Stops at the first failure and names the hop, because "no data in
# Grafana" has five possible causes and they need different fixes.
set -uo pipefail

TEMPO=${TEMPO:-http://localhost:3200}
PROM=${PROM:-http://localhost:9090}
GRAFANA=${GRAFANA:-http://localhost:3000}
COLLECTOR=${COLLECTOR:-http://localhost:4318}
COLLECTOR_METRICS=${COLLECTOR_METRICS:-http://localhost:8888}
DSN=${STDTEL_DSN:-postgresql://postgres:stdtel@localhost:5432/stdtel}
PG=${PG_CONTAINER:-deploy-postgres-1}

pass=0; fail=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n       symptom: %s\n' "$1" "$2"; fail=$((fail+1)); }

echo "1. containers"
for c in otel-collector tempo prometheus grafana postgres; do
  st=$(docker inspect -f '{{.State.Status}}' "deploy-$c-1" 2>/dev/null || echo missing)
  [ "$st" = running ] && ok "$c running" || bad "$c is $st" "run 'make up'; check 'docker compose logs $c'"
done

echo "2. endpoints reachable from the host"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$COLLECTOR/v1/traces" -X POST -H 'content-type: application/json' -d '{}')
[ "$code" = 200 ] || [ "$code" = 400 ] && ok "collector OTLP/HTTP ($code)" || bad "collector not answering ($code)" "port 4318 not published, or the container is unhealthy"
curl -sf --max-time 5 "$TEMPO/ready"        >/dev/null && ok "tempo ready"      || bad "tempo not ready" "tempo takes ~20s after start; retry before digging"
curl -sf --max-time 5 "$PROM/-/ready"       >/dev/null && ok "prometheus ready" || bad "prometheus not ready" "check deploy/prometheus.yml is mounted"
curl -sf --max-time 5 "$GRAFANA/api/health" >/dev/null && ok "grafana ready"    || bad "grafana not ready" "anonymous admin is enabled; no login needed"

echo "3. emit a span through the real hook"
export STDTEL_OTLP_ENDPOINT="$COLLECTOR" STDTEL_BRANCH=feature/SMOKE-1-check
export STDTEL_STATE_DIR=${STDTEL_STATE_DIR:-$(mktemp -d)}
SID="smoke-$(date +%s)"
if command -v stdtel-hook >/dev/null; then HOOK=stdtel-hook; else HOOK="$(dirname "$0")/../.venv/bin/stdtel-hook"; fi
echo "{\"session_id\":\"$SID\",\"cwd\":\"$PWD\"}" | "$HOOK" session-start
echo "{\"session_id\":\"$SID\",\"tool_name\":\"Skill\",\"tool_use_id\":\"s1\",\"tool_input\":{\"skill\":\"structured-logging\"}}" | "$HOOK" pre-tool-use
echo "{\"session_id\":\"$SID\",\"tool_name\":\"Skill\",\"tool_use_id\":\"s1\"}" | "$HOOK" post-tool-use
echo "{\"session_id\":\"$SID\"}" | "$HOOK" stop
[ -f "$STDTEL_STATE_DIR/$SID.json" ] && ok "hook wrote session state" || bad "no state file" "check 'stdtel-install where' and that the hook path is absolute"

echo "4. collector accepted it"
accepted=$(curl -s --max-time 5 "$COLLECTOR_METRICS/metrics" | awk '/^otelcol_receiver_accepted_spans/{s+=$2} END{print s+0}')
[ "${accepted:-0}" -gt 0 ] && ok "collector accepted $accepted span(s)" || bad "collector accepted 0 spans" "the exporter never reached it: check STDTEL_OTLP_ENDPOINT (OTEL_* is scrubbed from hooks)"
failed=$(curl -s --max-time 5 "$COLLECTOR_METRICS/metrics" | awk '/^otelcol_exporter_send_failed_spans/{s+=$2} END{print s+0}')
[ "${failed:-0}" -eq 0 ] && ok "collector exported with no failures" || bad "$failed span export(s) failed" "tempo unreachable from the collector; check the compose network"

echo "5. tempo made it searchable"
start=$(( $(date +%s) - 900 )); end=$(( $(date +%s) + 60 ))
found=0
for _ in 1 2 3 4 5 6 7 8; do
  found=$(curl -s --get "$TEMPO/api/search" --data-urlencode 'q={ name = "std.skill.invocation" }' \
          --data-urlencode "start=$start" --data-urlencode "end=$end" \
          | grep -o '"traceID"' | wc -l | tr -d ' ')
  [ "${found:-0}" -gt 0 ] && break
  curl -s -o /dev/null --max-time 6 --retry 1 --retry-delay 6 --retry-all-errors "$TEMPO/ready"
done
[ "${found:-0}" -gt 0 ] && ok "tempo returned $found trace(s)" || bad "tempo returned nothing" "ingestion lag (retry), OR the query had no start/end — without them Tempo silently returns zero"

echo "6. prometheus scraped the span metrics"
series=$(curl -s --get "$PROM/api/v1/query" --data-urlencode 'query=traces_span_metrics_calls_total' | grep -o '"metric"' | wc -l | tr -d ' ')
[ "${series:-0}" -gt 0 ] && ok "$series span-metric series" || bad "no span metrics" "spanmetrics connector or remote-write is misconfigured; scrape interval may not have elapsed"

echo "7. postgres schema and loader"
cols=$(docker exec "$PG" psql -U postgres -d stdtel -tAc \
  "SELECT count(*) FROM information_schema.columns WHERE table_name='skill_invocation';" 2>/dev/null)
[ "${cols:-0}" -gt 0 ] && ok "schema loaded ($cols columns)" || bad "no schema" "the init script runs only on first volume create; see 'resetting' in docs/local-stack.md"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
