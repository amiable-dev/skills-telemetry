---
title: "ADR-006: Langfuse as an optional trace backend, not a replacement for the warehouse"
status: accepted
date: 2026-09-10
tags: [adr, langfuse, collector, observability]
links: ["001-distribution-and-capture-surface.md", "003-hook-execution-constraints.md", "../local-stack.md", "../../collector/overlay-langfuse.yaml"]
verified: "2026-09-10 — end to end against self-hosted Langfuse v4: hook -> collector -> Langfuse, with the transformed keys confirmed filterable in ClickHouse"
---

## Context

The team already self-hosts Langfuse. The question is whether this project should use it rather than
carry its own Tempo, Prometheus, Grafana and Postgres.

Langfuse is an LLM-observability platform: traces, observations, sessions, users, scores, cost views.
Three properties decide how much of our stack it can absorb.

1. **It cannot capture.** Nothing in Langfuse can observe a Claude Code skill load; that is what the
   hooks exist for. The capture layer is out of scope for this decision.
2. **Custom attributes are not queryable by default.** From their OTel docs: unmapped attributes land
   in `metadata.attributes` as a catch-all blob, and only `langfuse.trace.metadata.*` becomes a
   filterable key. **Our entire schema is `std.*`**, so a naive export produces a UI that shows the
   traces and cannot group or filter by skill, version, plugin, ticket, team or harness.
3. **Its unit of analysis is the trace.** Our primary metric is first-time OPA policy pass rate joined
   across `ticket → pull_request → policy_result`, keyed on `run_seq` — which CI run this was.

## Options considered

- **Replace the whole stack with Langfuse.** Rejected on point 3. A PR is not a trace; one PR
  aggregates many sessions, and `run_seq` is a property of a CI run, not of an LLM call. The join, the
  with/without cohorts and the crossover analysis are relational SQL over a warehouse.
- **Export `std.*` unchanged and rely on the metadata blob.** Rejected: it yields a pipeline that
  looks healthy and answers no questions — the exact failure mode this project has spent its history
  removing.
- **Rename our attributes to `langfuse.trace.metadata.*` at the source.** Rejected: it would couple the
  span schema to one vendor, and break Tempo, the warehouse loader, the spanmetrics dimensions and
  every documented query.
- **Use Langfuse scores for `policy_result`.** Rejected for now. Scores attach to traces, and the
  grain is wrong: a policy result belongs to a PR and a CI run. The trend UI is attractive, but
  storing the metric at the wrong grain would make the primary metric harder to compute, not easier.
- **Replace Tempo but keep everything else, as a hard swap.** Rejected as premature: unverified
  ingestion (below) and no migration path for existing traces. Fan-out first, remove Tempo later if it
  proves redundant.

## Decision

1. **Langfuse is an optional additional trace exporter, selected by a collector overlay.** The
   collector merges a second `--config` over the base; `overlay-none.yaml` is the default and is a
   deliberate no-op, so the Langfuse exporter does not exist unless asked for.
2. **A transform duplicates `std.*` into `langfuse.trace.metadata.*`** — duplicates, never moves, so
   Tempo, the warehouse and spanmetrics are untouched. `session.id` is also mapped to
   `langfuse.session.id` for native session grouping.
3. **Tempo, Prometheus and the warehouse stay.** Langfuse is for looking at a session; the warehouse
   is for answering the question the project exists to answer.
4. **Langfuse runs under a compose profile** with remapped ports — upstream defaults collide with
   Grafana (3000), our warehouse Postgres (5432) and Prometheus (9090).
5. **Credentials live in `deploy/.env`, which is gitignored**, with `deploy/.env.example` committed.
   The collector reads `${env:LANGFUSE_AUTH}`.

## Consequences

- Adopting Langfuse is reversible and costs one environment variable. Removing it is the same.
- **A Langfuse outage cannot affect the rest of the pipeline.** Verified with Langfuse down:
  `otelcol_exporter_sent_spans{exporter="otlp/tempo"} 2` with zero failures, while
  `otlphttp/langfuse` recorded 2 failures and 0 sent. The exporters are independent; the hook still
  exited 0 and the span still reached Tempo and Postgres.
- **Verified end to end.** With Langfuse reachable: `otelcol_exporter_sent_spans{otlphttp/langfuse} 4`
  with zero failures, six rows in `events_full` under project `stdtel` carrying our session ids, and
  the transform's keys promoted to top-level filterable metadata — `skill_name`, `skill_version`,
  `standard_id`, `trigger`, `ticket_id`, `team`, `harness`, `repo` — while the raw `std.*` attributes
  sit in the nested `attributes.*` blob. That contrast is the decision in this ADR, observed directly.
- **Span identity changes on the way in.** Langfuse names an observation from `gen_ai.tool.name`, so
  `std.skill.invocation` appears as `Skill`. Anything correlating the two backends by span name must
  account for that; `session.id` and `std.prompt.id` correlate cleanly and are the better keys.
- Two attribute vocabularies now travel on every span. That is the price of a queryable Langfuse view,
  and the transform is one place to change.
- Self-hosting is not free: Langfuse v4 needs web, worker, Postgres, ClickHouse, Redis and MinIO —
  six containers on top of our five.

### Known limitations

- **Langfuse v4 writes the new `events_*` model, and `GET /api/public/traces` reads the legacy
  tables.** Ingested spans are visible in the UI and in `events_core`/`events_full`, but that REST
  endpoint returns an empty list until the "DUAL WRITE" backfill job populates `traces`/`observations`.
  Anything scripted against that endpoint will look like a broken pipeline when it is not — query the
  v4 model, or drop the `x-langfuse-ingestion-version: 4` header and accept up to ten minutes of lag.
- **Memory.** Langfuse v4 adds six containers and ClickHouse alone holds ~700 MiB; on a 2 GiB Docker
  VM `langfuse-web` is OOM-killed during boot (exit 137). Budget 8 GiB.
- `LANGFUSE_AUTH` is derived from two keys, so it cannot live in `.env` and must be passed through to
  the collector explicitly. Missing, it produces HTTP 401 — which is invisible while Langfuse is down,
  because the DNS failure masks it.
- The attribute list in the transform is **hand-maintained**. A new `std.*` attribute will not appear
  in Langfuse until it is added there, and nothing fails when it is forgotten.
- `x-langfuse-ingestion-version: 4` is set because ingestion otherwise lags up to ten minutes; that
  header is version-specific and will need review on upgrade.
- Self-hosted Langfuse requires v3.22.0+ for the OTel path.
- Whether Langfuse's metrics API can aggregate by metadata key is **unconfirmed**; if it cannot,
  cost-per-skill stays in Postgres, which is where it is computed today anyway.

## Related

The overlay mechanism is general: any additional exporter can ship as an overlay without touching the
base pipeline. `docs/local-stack.md` documents running the profile.
