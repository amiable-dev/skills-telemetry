---
title: "ADR-016: Count rare events where a first occurrence is visible"
status: accepted
date: 2026-10-09
tags: [adr, metrics, prometheus, spanmetrics, dashboards]
links: ["009-artefact-activation-as-the-unit-of-capture.md", "010-containment-and-scope.md", "014-harness-native-telemetry.md"]
---

## Context

On 2026-10-09 the operational dashboard showed no skill activity over 24 hours in which skills ran.
Prometheus held a series for every skill. **Every one was flat for the whole window**, at 1 (or 24).
So `increase()` was 0, `rate()` was 0, and every p50 was NaN. "Error rate by skill" read "No skill ran
in this range" while skills had run.

Two causes compound:
- **A counter's first appearance is invisible to `rate()` and `increase()`.** Spanmetrics creates a
  series at 1 the first time a combination of labels is seen. Prometheus has no earlier sample, so
  that first 1 is not an increase. Each collector restart starts every series again.
- **stdtel's events are rare, and its dimensions make them rarer per series.** A skill runs a few
  times a day. Spanmetrics keys series on 14 dimensions, including `std.scope.key`, the branch hash
  (ADR-010), which rolls every iteration. Most series are therefore born at 1 and never move. Errors,
  sub-agents and compactions behave the same way.

This is structural, not a dashboard bug. Any panel built on `rate()` of spanmetrics undercounts
exactly the events this project exists to see.

**Measured 2026-10-09.** One cumulative counter sample (value 1, `StartTimeUnixNano` 90 s earlier) was
sent through two ingestion paths, then `increase(...[10m])` was queried:

| path | Prometheus 3.2.0 (deployed) | Prometheus 3.15.0 |
|---|---|---|
| collector `prometheusremotewrite` (deployed) | samples `[1, 1]`, increase **0** | one sample, increase **empty** |
| Prometheus native OTLP receiver, `--enable-feature=created-timestamp-zero-ingestion` | samples `[1, 1]`, increase **0** | samples `[0 @ start, 1]`, increase **1.22** |

Only the last cell counts a first occurrence. It writes a zero at the series' start time. (The 1.22 is
`increase()` extrapolating across the window, not a second event.) The feature flag is accepted on
3.2.0 but does nothing on the OTLP path there.

## Options considered

1. **Keep Prometheus, change the panels to `max_over_time` / `changes()` tricks.** **Rejected.**
   Neither recovers a first occurrence. Each trades one undercount for a different one, and every
   panel author would have to know the trick.
2. **Delta temporality from the collector.** **Rejected.** Prometheus stores cumulative series. The
   `cumulativetodelta` / `deltatocumulative` round trip reintroduces the same first-sample problem one
   hop later, and it adds state to the collector.
3. **Move every count out of Prometheus into Tempo (TraceQL metrics) or the warehouse.**
   **Rejected as the primary path.** TraceQL metrics needs the metrics-generator's local blocks, which
   this stack does not run. The warehouse lags by the loader interval (ADR-015). Both remain right for
   per-name tables in a time range (decision 3).
4. **Upgrade Prometheus to a release whose OTLP receiver ingests start timestamps, and send metrics
   over OTLP instead of remote write.** Chosen. It is the only path measured to count a first
   occurrence, and it fixes every existing `rate()` panel without rewriting it.

## Decision

1. **Prometheus 3.15 receives metrics over its native OTLP endpoint and writes a zero at each series'
   start.**
   - **Image:** `prom/prometheus:v3.15.0`, pinned by digest in compose. Every upgrade is gated on the
     test in work item W1.
   - **Flags:** `--web.enable-otlp-receiver` and `--enable-feature=created-timestamp-zero-ingestion`,
     beside the existing ones.
   - **Prometheus config:** `otlp.translation_strategy: UnderscoreEscapingWithSuffixes`, which keeps
     today's names (`traces_span_metrics_calls_total`, `std_skill_name`). Today's labels are datapoint
     attributes (spanmetrics dimensions), not resource attributes, so they need no promotion and no
     `target_info` join. `promote_resource_attributes` is left empty, and a test proves a
     `std_skill_name` selector resolves without a join.
   - **Collector:** the metrics pipeline exports `otlphttp` with
     `metrics_endpoint: http://prometheus:9090/api/v1/otlp/v1/metrics`. That is set in full, because
     the exporter appends `/v1/metrics` to a bare `endpoint`. It keeps the collector's default retry
     and queue. `prometheusremotewrite` is removed.
   - **Out-of-order:** the backfilled zero at the start time must be accepted, and the real-image test
     asserts it is. `tsdb.out_of_order_time_window` is set only if that test needs it.
2. **`std.scope.key` leaves the spanmetrics dimensions, and the remaining cardinality is bounded.**
   - `std.scope.key` is per-branch and unbounded over time. ADR-010 already kept `std.scope.id` out for
     the same reason, and the same guard test covers both. `std.scope.name` stays: the catalogue bounds
     it.
   - Spanmetrics keeps `dimensions_cache_size` at its default of 1000 and sets `metrics_expiration: 24h`.
     A series not seen for a day leaves the connector's memory; when it returns it starts again at a new
     start time, which decision 1 now counts.
   - The operational dashboard gains a "Prometheus active series" panel
     (`prometheus_tsdb_head_series`), with a 10,000 threshold, so growth is visible.
3. **Per-name tables in a time range read stores that are complete per event, not rates.**
   - "Skills invoked", "MCP calls" and "Tool calls" already read Loki. "Skills invoked" adds Claude
     Code's `skill_activated` events, so skills typed as `/name` appear.
   - A new "Spend by scope" panel reads the warehouse, the only store that joins Claude Code's requests
     to stdtel's scope by `prompt_id` (ADR-014 decision 7). On 2026-10-09 that join put $228.16 of 72
     hours under `epic-loop` and $51.55 under no scope; no current panel can show that.
   - The panel names the warehouse's age (ADR-015's `warehouse freshness`). It also shows the share of
     spend whose `prompt_id` joins no stdtel turn, as its own row rather than folded into "no scope".
4. **Empty states say what is empty.**
   - A series that has never been seen has no data. A first-seen series now starts at 0. A ratio with
     no errors is 0, not "No skill ran".
   - Legends name their series, not the expression. A spend total is not coloured as an alarm.
   - There are no alert rules in this stack today. Any added later must not rely on `absent()` for a
     series that decision 1 now creates at 0.
   - A count assertion uses `increase(...) > 0`, never equality: `increase()` extrapolates.

### Work items
- **W1. Prometheus 3.15 over OTLP.** Covers decision 1. The real-image test matrix:
  - first appearance;
  - three increments;
  - a collector restart mid-window, which starts a new series;
  - first appearance at the start, middle and end of the query window;
  - a retried, delayed batch;
  - every existing dashboard PromQL expression resolves with the same names and labels.
- **W2. Drop `std.scope.key` and bound cardinality.** Covers decision 2: guard test,
  `metrics_expiration`, and the active-series panel.
- **W3. Per-event tables.** Covers decision 3: `skill_activated` in "Skills invoked", and "Spend by
  scope" with its age and its unjoined row.
- **W4. Empty states.** Covers decision 4: empty-state, legend and threshold fixes on both dashboards.
- **W5. Rollback.** If W1's test fails on a future upgrade, the stack stays on the last passing digest.
  - If OTLP ingestion has to be abandoned, the collector reverts to `prometheusremotewrite`, and every
    rate panel carries a description stating its first-occurrence undercount.
  - Exact counts then come only from the per-event tables in decision 3.
  - Nothing claims a threshold or query trick restores a first occurrence. None can.

## Consequences

- Every existing `rate()` and `increase()` panel starts counting first occurrences without being
  rewritten, and a collector restart no longer erases a series' first event.
- Prometheus is one version family newer, pinned by digest, and a feature flag is required. The flag
  is experimental in 3.15: a release note could change it, which is why decision 1 makes it a test.
- Series cardinality falls, because one dimension goes.

### Known limitations

- **Start-timestamp ingestion is experimental upstream.** Pinned and tested, but a future upgrade
  could change its behaviour. The test is what notices.
- **`increase()` extrapolates.** A single event over a window can read 1.2 rather than 1. Panels that
  need exact counts in a range use the per-event stores (decision 3), not `increase()`.
- **Data already in Prometheus keeps its undercount.** Nothing is backfilled.
- **Unverified on remote write 2.0.** The collector at 0.118 sends remote write 1.0, which has no start
  timestamps. Remote write 2.0 is a non-goal here, tracked as a follow-up; this ADR does not depend on it.
- **One owner.** This is a single-maintainer project. The maintainer owns tracking the experimental flag
  upstream, and the W1 test is what makes a change visible without anyone remembering to look.

## Review

Council (llm-council, balanced tier, 2 of 4 models responding), 2026-10-09, on the first draft:
**approved, confidence 0.78, with required changes.**
- It found the diagnosis measurement-backed and the chosen option the only demonstrated path.
- It asked for:
  - a wider experiment than one sample;
  - explicit translation, endpoint and flag settings;
  - digest pinning;
  - a rollback item;
  - real cardinality controls;
  - a note on absence-based alerts.

This revision makes each change in decisions 1 to 4 and work items W1 to W5.

Council on the revision (all 4 models): **approved, confidence 0.85, no remaining blocking gaps.** The
one dissent read `metrics_endpoint` as doubling the path; the other graders found that a misreading.
A per-signal endpoint is used verbatim.
