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

# Several hops are eventually-consistent: Tempo needs ~20s to accept traffic,
# and span metrics must flush through the connector, remote write and a scrape
# before they are queryable. Checking once makes a cold start look broken, and a
# verification tool that cries wolf gets ignored. Retry, then report.
retry() {   # retry <attempts> <delay> <command...>
    attempts=$1; delay=$2; shift 2
    i=0
    while [ "$i" -lt "$attempts" ]; do
        if "$@"; then return 0; fi
        i=$((i + 1))
        [ "$i" -lt "$attempts" ] && sleep "$delay"
    done
    return 1
}
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n       symptom: %s\n' "$1" "$2"; fail=$((fail+1)); }

echo "1. containers"
for c in otel-collector tempo prometheus grafana postgres; do
  st=$(docker inspect -f '{{.State.Status}}' "deploy-$c-1" 2>/dev/null || echo missing)
  [ "$st" = running ] && ok "$c running" || bad "$c is $st" "run 'make up'; check 'docker compose logs $c'"
done

echo "2. endpoints reachable from the host"
# Every service here is starting concurrently, so all four checks retry. Only
# the collector's is odd-looking: it has no health endpoint, so an empty OTLP
# POST is the probe, and 400 ("listening, rejected the empty body") is success.
collector_up() {
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$COLLECTOR/v1/traces" \
        -X POST -H 'content-type: application/json' -d '{}')
    [ "$code" = 200 ] || [ "$code" = 400 ]
}
tempo_ready()   { curl -sf --max-time 5 "$TEMPO/ready"        >/dev/null; }
prom_ready()    { curl -sf --max-time 5 "$PROM/-/ready"       >/dev/null; }
grafana_ready() { curl -sf --max-time 5 "$GRAFANA/api/health" >/dev/null; }

retry 12 5 collector_up && ok "collector OTLP/HTTP ($code)" \
  || bad "collector not answering after 60s ($code)" "port 4318 not published, or the container is unhealthy"
retry 12 5 tempo_ready   && ok "tempo ready"      || bad "tempo not ready after 60s" "check 'docker compose logs tempo'"
retry 12 5 prom_ready    && ok "prometheus ready" || bad "prometheus not ready after 60s" "check deploy/prometheus.yml is mounted"
retry 12 5 grafana_ready && ok "grafana ready"    || bad "grafana not ready after 60s" "anonymous admin is enabled; no login needed"

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

# A second invocation, from a second set of processes. Not redundant: the whole
# of #42 was that each hook process opened its own series, so one emission looks
# identical whether the counter can accumulate or not. Step 6 checks it did.
SID2="smoke-$(date +%s)-b"
echo "{\"session_id\":\"$SID2\",\"cwd\":\"$PWD\"}" | "$HOOK" session-start
echo "{\"session_id\":\"$SID2\",\"tool_name\":\"Skill\",\"tool_use_id\":\"s2\",\"tool_input\":{\"skill\":\"structured-logging\"}}" | "$HOOK" pre-tool-use
echo "{\"session_id\":\"$SID2\",\"tool_name\":\"Skill\",\"tool_use_id\":\"s2\"}" | "$HOOK" post-tool-use
echo "{\"session_id\":\"$SID2\"}" | "$HOOK" stop

echo "4. collector accepted it"
accepted=$(curl -s --max-time 5 "$COLLECTOR_METRICS/metrics" | awk '/^otelcol_receiver_accepted_spans/{s+=$2} END{print s+0}')
[ "${accepted:-0}" -gt 0 ] && ok "collector accepted $accepted span(s)" || bad "collector accepted 0 spans" "the exporter never reached it: check STDTEL_OTLP_ENDPOINT (OTEL_* is scrubbed from hooks)"
failed=$(curl -s --max-time 5 "$COLLECTOR_METRICS/metrics" | awk '/^otelcol_exporter_send_failed_spans/{s+=$2} END{print s+0}')
[ "${failed:-0}" -eq 0 ] && ok "collector exported with no failures" || bad "$failed span export(s) failed" "tempo unreachable from the collector; check the compose network"

echo "5. tempo made it searchable"
start=$(( $(date +%s) - 900 )); end=$(( $(date +%s) + 60 ))
found=0
for _ in 1 2 3 4 5 6 7 8; do
  found=$(curl -s --get "$TEMPO/api/search" --data-urlencode 'q={ name = "std.artefact.activation" }' \
          --data-urlencode "start=$start" --data-urlencode "end=$end" \
          | grep -o '"traceID"' | wc -l | tr -d ' ')
  [ "${found:-0}" -gt 0 ] && break
  curl -s -o /dev/null --max-time 6 --retry 1 --retry-delay 6 --retry-all-errors "$TEMPO/ready"
done
[ "${found:-0}" -gt 0 ] && ok "tempo returned $found trace(s)" || bad "tempo returned nothing" "ingestion lag (retry), OR the query had no start/end — without them Tempo silently returns zero"

echo "6. prometheus scraped the span metrics"
have_metrics() {
    series=$(curl -s --get "$PROM/api/v1/query" \
        --data-urlencode 'query=traces_span_metrics_calls_total' \
        | grep -o '"metric"' | wc -l | tr -d ' ')
    [ "${series:-0}" -gt 0 ]
}
# the span emitted above must cross the connector, remote write and a scrape
retry 12 5 have_metrics && ok "$series span-metric series" \
  || bad "no span metrics after 60s" "check otelcol_exporter_send_failed_metric_points at :8888, and that the spanmetrics connector is in the traces pipeline"

# The series existing is not the same as the series being usable. #42: every hook
# process carried a generated service.instance.id, which becomes the `instance`
# label, so every span landed in a fresh series that reached 1 and stopped. Every
# rate() panel read zero at any volume while the legend looked healthy.
promq() { curl -s --get "$PROM/api/v1/query" --data-urlencode "query=$1" \
          | sed -n 's/.*"value":\[[^,]*,"\([^"]*\)"\].*/\1/p'; }
counter_accumulates() {
    peak=$(promq 'max(traces_span_metrics_calls_total{span_name="std.artefact.activation"})')
    peak=${peak%%.*}
    [ "${peak:-0}" -ge 2 ]
}
retry 12 5 counter_accumulates && ok "counter reached $peak across separate hook processes" \
  || bad "counter never exceeded ${peak:-0} after two invocations" \
        "each hook process is opening its own series - check service.instance.id is pinned (stdtel/identity.py) and that the metrics pipeline drops it (#42)"

instances=$(promq 'count(count by (instance) (traces_span_metrics_calls_total{span_name="std.artefact.activation"}))')
instances=${instances%%.*}
[ "${instances:-0}" -le 1 ] && ok "one series per skill, not one per process" \
  || bad "$instances distinct instance labels" "cardinality is growing per hook process; stale series clear after ~5m, so re-run if you have just fixed it (#42)"

echo "7. postgres schema and loader"
cols=$(docker exec "$PG" psql -U postgres -d stdtel -tAc \
  "SELECT count(*) FROM information_schema.columns WHERE table_name='skill_invocation';" 2>/dev/null)
[ "${cols:-0}" -gt 0 ] && ok "schema loaded ($cols columns)" || bad "no schema" "the init script runs only on first volume create; see 'resetting' in docs/local-stack.md"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
