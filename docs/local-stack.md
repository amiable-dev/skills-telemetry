# The local metrics stack

Five containers: an OpenTelemetry collector, Tempo for traces, Prometheus for metrics, Grafana on top
of both, and Postgres for the warehouse. `make up` starts them; nothing needs configuring afterwards.

```bash
make up          # start
make down        # stop (data survives)
mise run smoke   # verify every hop and name the one that broke
```

## Endpoints and credentials

| service | URL | credentials | notes |
|---|---|---|---|
| Grafana | http://localhost:3000 | **none — anonymous admin** | Prometheus and Tempo are already provisioned as datasources; do not add them |
| Tempo | http://localhost:3200 | none | `/ready`, `/api/search`, `/api/traces/{id}` |
| Prometheus | http://localhost:9090 | none | span metrics arrive by remote write |
| Collector (OTLP) | http://localhost:4318 | none | gRPC on 4317; point `STDTEL_OTLP_ENDPOINT` here |
| Collector (metrics) | http://localhost:8888/metrics | none | the collector's own counters — the fastest way to see whether it received anything |
| Postgres | localhost:5432 | user `postgres`, password `stdtel`, db `stdtel` | `postgresql://postgres:stdtel@localhost:5432/stdtel` |

```bash
export STDTEL_DSN=postgresql://postgres:stdtel@localhost:5432/stdtel
psql "$STDTEL_DSN" -c '\dt'
```

`make up` and `make down` detect whether you have the `docker compose` plugin or the standalone
`docker-compose` binary. Invoking compose directly, pick the one you actually have — this machine had
only the standalone binary, and `docker compose up` failed in a way that looked like a compose-file
error.

## From nothing to a queried span

```bash
make up                                   # 1. start
mise run smoke                            # 2. verify the whole path (see the ladder below)

# 3. emit from a real hook
export STDTEL_OTLP_ENDPOINT=http://localhost:4318 STDTEL_BRANCH=feature/PLAT-1-demo
echo '{"session_id":"demo","cwd":"'$PWD'"}' | stdtel-hook session-start
echo '{"session_id":"demo","tool_name":"Skill","tool_use_id":"1","tool_input":{"skill":"structured-logging"}}' | stdtel-hook pre-tool-use
echo '{"session_id":"demo","tool_name":"Skill","tool_use_id":"1"}' | stdtel-hook post-tool-use
echo '{"session_id":"demo"}' | stdtel-hook stop

# 4. read it back — note start/end, they are NOT optional
curl -s --get http://localhost:3200/api/search \
  --data-urlencode 'q={ name = "std.skill.invocation" }' \
  --data-urlencode "start=$(( $(date +%s) - 900 ))" --data-urlencode "end=$(date +%s)"

# 5. load traces into the warehouse, then query it
python warehouse/load_traces.py --tempo http://localhost:3200 --dsn "$STDTEL_DSN" --since 24h
psql "$STDTEL_DSN" -c 'SELECT skill_name, skill_version, ticket_id, tail_tokens FROM skill_invocation;'
```

Then open Grafana at :3000 — the scorecard dashboard is provisioned.

## The verification ladder

`mise run smoke` walks the path in the order data flows and stops at the first broken hop, because
"nothing in Grafana" has at least five causes and they need different fixes. Each hop, what proves it,
and what it means when it fails:

| # | hop | proof | failure means |
|---|---|---|---|
| 1 | containers up | `docker inspect` status | run `make up`; check that container's logs |
| 2 | endpoints reachable | `/ready`, `/api/health`, an OTLP POST | a port is not published, or the service is still starting — Tempo needs ~20s |
| 3 | the hook runs | a session state file appears | the hook is not on PATH: check `stdtel-install where` |
| 4 | collector received | `otelcol_receiver_accepted_spans` > 0 | the exporter never reached it. Check `STDTEL_OTLP_ENDPOINT` — **`OTEL_*` is scrubbed from hook subprocesses**, so the OTel variable cannot configure this |
| 5 | collector exported | `otelcol_exporter_send_failed_spans` == 0 | Tempo unreachable *from inside* the compose network |
| 6 | Tempo searchable | a search returns a trace | ingestion lag, or a missing time range — see below |
| 7 | Prometheus scraped | `traces_span_metrics_calls_total` has series | spanmetrics or remote-write misconfigured, or the scrape interval has not elapsed |
| 8 | Postgres schema | `skill_invocation` has columns | the init script ran on an existing volume — see resetting |

The counters at hop 4 are the single most useful thing here. `otelcol_receiver_accepted_spans` tells
you whether the problem is upstream of the collector (your config) or downstream (the stack).

## Troubleshooting

### Tempo returns nothing

**Tempo silently returns zero results when a search has no `start` and `end`.** No error, no warning —
an empty result identical to an empty pipeline. This cost real debugging time during development, and
it is the first thing to check when a query "finds nothing":

```bash
# wrong — always returns nothing
curl -s 'http://localhost:3200/api/search?q=%7B%20name%3D%22std.skill.invocation%22%20%7D'

# right
curl -s --get http://localhost:3200/api/search \
  --data-urlencode 'q={ name = "std.skill.invocation" }' \
  --data-urlencode "start=$(( $(date +%s) - 3600 ))" --data-urlencode "end=$(date +%s)"
```

Second cause: **ingestion lag**. A span is accepted well before it is searchable, so a loader run
immediately after emitting will find nothing and report `0 rows` truthfully. Retry before concluding.
`mise run smoke` polls for this reason.

### Spans emitted, nothing arrives

Check hop 4. If `otelcol_receiver_accepted_spans` is 0 the collector never saw them, and the cause is
almost always the endpoint: `OTEL_EXPORTER_OTLP_ENDPOINT` **cannot** configure a hook, because Claude
Code strips `OTEL_*` from every subprocess it spawns. Use `STDTEL_OTLP_ENDPOINT`
([ADR-003](adrs/003-hook-execution-constraints.md)).

### The loader loads zero rows

Almost always ingestion lag (above). It is also worth checking you are looking at the right window —
`--since 24h` bounds the Tempo search.

## Resetting

The Postgres schema is mounted as an init script, and **init scripts run only when the data directory
is created**. After changing `warehouse/schema.sql`, restarting is not enough:

```bash
make down
docker volume rm deploy_tempo-data 2>/dev/null || true   # traces
docker-compose -f deploy/docker-compose.yml down -v      # everything, including the database
make up
```

To apply a schema change without losing data, run the DDL yourself:

```bash
docker exec -i deploy-postgres-1 psql -U postgres -d stdtel < warehouse/schema.sql
```

`schema.sql` is written with `CREATE TABLE IF NOT EXISTS`, so it is safe to re-run — but it will not
alter an existing table. A changed column needs its own `ALTER`.

## Langfuse (optional)

Self-hosted Langfuse can receive the same traces as an additional destination — see
[ADR-006](adrs/006-langfuse-as-an-optional-trace-backend.md) for why it supplements the stack rather
than replacing it.

```bash
cp deploy/.env.example deploy/.env     # local-only credentials; the file is gitignored
make up-langfuse                       # base stack + the langfuse profile
open http://localhost:3001             # remapped: 3000 is Grafana
```

Ports are remapped because upstream collides with Grafana (3000), our warehouse Postgres (5432) and
Prometheus (9090). Langfuse's own Postgres, ClickHouse, Redis and MinIO are not published at all.

**How it is wired.** The collector merges a second `--config` over the base. `overlay-none.yaml` is
the default and does nothing; `overlay-langfuse.yaml` adds the exporter *and* a transform that
duplicates `std.*` into `langfuse.trace.metadata.*`. That transform is not optional: Langfuse only
makes attributes filterable under that prefix, so without it every trace arrives with its skill,
ticket and team in an unqueryable blob.

**It cannot break the rest of the pipeline.** Exporters are independent — with Langfuse down, Tempo
recorded 2 spans sent and 0 failed while the Langfuse exporter recorded 2 failed and 0 sent. Check
both with `curl -s localhost:8888/metrics | grep otelcol_exporter`.

> **Memory.** Langfuse v4 adds six containers, and ClickHouse alone holds ~700 MiB. On a 2 GiB Docker
> VM `langfuse-web` is OOM-killed during boot (exit 137). Give the VM 8 GiB — for colima,
> `colima stop && colima start --memory 8`, which restarts every container on the machine.
> **Ingestion into Langfuse has not been verified on this machine for that reason.**

## What is provisioned

- **Grafana datasources**: Prometheus (`http://prometheus:9090`) and Tempo (`http://tempo:3200`),
  from `deploy/grafana/provisioning/datasources/ds.yaml`. Anonymous access is admin, so there is no
  login step and no password to find.
- **Grafana dashboard**: the skill scorecard, from `deploy/grafana/provisioning/dashboards/`.
- **Collector pipeline**: content dropped, `gen_ai.*` normalised, Copilot tool-calls mapped onto
  `std.skill.*`, user identifiers hashed, then fanned out to Tempo and to Prometheus via spanmetrics.
- **Postgres**: `warehouse/schema.sql` on first create.

Service ports are published to the host, so anything on your machine can reach them. This stack has no
authentication anywhere and is for local use only.
