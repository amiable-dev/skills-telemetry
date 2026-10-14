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
     *(Corrected 2026-10-14: that setting was unused and bounds nothing. See the amendment below and
     #195.)*
     A series not seen for a day leaves the connector's memory; when it returns it starts again at a new
     start time, which decision 1 now counts.
   - The operational dashboard gains a "Prometheus active series" panel
     (`prometheus_tsdb_head_series`), with a 10,000 threshold, so growth is visible.
3. **Per-name tables in a time range read stores that are complete per event, not rates.**
   - "Skills invoked", "MCP calls" and "Tool calls" already read Loki. "Skills invoked" adds Claude
     Code's `skill_activated` events, so skills typed as `/name` appear.
     *(Corrected 2026-10-14, #159: it reads `skill_activated` **alone**. That event also fires once for
     each Skill tool call, so adding it to the `tool_result` count would count those twice. Measured
     over six days: 16 Skill tool calls matched 16 activations triggered `claude-proactive` or
     `nested-skill`, and 30 typed activations made no tool call.)*
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
- **Remote write is no longer used.** The collector at 0.118 sent remote write 1.0, which has no start
  timestamps. Since #158 metrics go over OTLP, so remote write 2.0 is moot here.
- **One owner.** This is a single-maintainer project. The maintainer owns tracking the experimental flag
  upstream, and the W1 test is what makes a change visible without anyone remembering to look.

## Amendment (2026-10-14, #158): the start time must be the series' own

Decision 1 was right but not sufficient. A zero at the start time only counts a first occurrence if the
start time is close to that occurrence, and collector-contrib 0.118 did not provide one.

**What was measured.** The test was the W1 matrix in `tests/test_counting_first_occurrences.py`, using the
shipped images and configs. Each probe series is born after the collector has run for longer than the
30 s query window, as on any collector that has been up for a while.

| collector | Prometheus | first occurrence `increase()` | first two samples |
|---|---|---|---|
| 0.118.0 | 3.2.0, remote write | 0 | not measured (remote write 1.0 carries no start time) |
| 0.118.0 | 3.15.0, OTLP + start-timestamp ingestion | 0 | `1, 1`: the zero was written > 40 s earlier, outside the window |
| 0.126.0 | 3.15.0, OTLP + start-timestamp ingestion | > 0, in all 20 cases | `0, 1`, under 10 s apart |

**Why.** In 0.118 the spanmetrics connector keeps one start timestamp per *resource*, taken when the
resource is first seen. Because of `resource_metrics_key_attributes: [service.name]`, that is one start time
for every stdtel series, from when the collector first saw stdtel. On 2026-10-13, `probe-a` and `probe-b`,
sent 8 s apart, both carried the same start time. The 2026-10-09 measurement in the Context missed this
because its sample set its own start time 90 s back.

From 0.124 the connector keeps a start time per series (open-telemetry/opentelemetry-collector-contrib#35994).
Images for 0.124 and 0.125 were never published, so 0.126.0 is the first image. *(Corrected 2026-10-14,
#161: this first cited 0.125 and #38990, from a source search. The release notes name #35994 in 0.124.)*

**Also in 0.124, found in #161:** the connector starts each new `calls_total` series at 0 itself
(#38537). Its first export carries the value 0 and its second carries 1 (debug exporter, 2026-10-14). So a
first occurrence in `calls_total` is counted on *any* path that delivers both points. That includes
Prometheus without the flag, and remote write. The duration histogram gets no such zero, so its first
occurrence depends on Prometheus's start-time ingestion alone:

| path (collector 0.126, Prometheus 3.15) | `calls_total` first occurrence | duration histogram first occurrence |
|---|---|---|
| OTLP, with `created-timestamp-zero-ingestion` (shipped) | counted | counted |
| OTLP, without the flag | counted | 0 |
| remote write (the W5 fallback) | counted | 0 |

**Decision 1 now also requires:**
- **Collector:** `otel/opentelemetry-collector-contrib:0.126.0`, pinned by digest in compose. Both test
  harnesses read the image from compose, so the privacy tests and the W1 matrix run on what ships.
- **The images are pinned by multi-arch index digest** (six platforms each, checked on Docker Hub), so the
  amd64 CI runner and an arm64 laptop pull the same release. The W1 matrix runs wherever docker is present,
  including CI on `ubuntu-latest`. Without docker it skips, like the privacy tests.
- **The collector version is now load-bearing.** A downgrade below 0.124 silently restores the undercount.
  The W1 test fails on it, which the 0.118 row above shows.
- **No out-of-order window is needed.** A retried batch, delivered after Prometheus has accepted later
  samples from other series, was accepted with its zero. `tsdb.out_of_order_time_window` stays unset
  (decision 1, last bullet).

**The zero-ingestion flag is load-bearing for the duration histograms.** Mutation testing in #158 found that
removing it left `calls_total` counting. That was first written up as "3.15.0 did not need it". #161
measured why: the zero in `calls_total` comes from the connector (above), so the #158 tests, which read only
`calls_total`, could not see the flag. `test_a_first_duration_is_counted` now reads the histogram, and fails
without the flag. `translation_strategy` is 3.15.0's default value, so only a string check guards it.

**Option 3 is unchanged.** Exact per-name counts in a time range read Loki or the warehouse (decision 3).
`increase()` still extrapolates, and data already in Prometheus keeps its undercount.

**Also found:** contrib 0.125 deprecated `dimensions_cache_size` as unused. Decision 2's cardinality bound was
never in force. The real control is `aggregation_cardinality_limit`, tracked in #195.

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
