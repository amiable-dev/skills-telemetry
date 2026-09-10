---
name: stdtel-query
description: Query standards-telemetry data to answer questions about skill cost, token usage, cache-hit rate, and delivery outcomes — tokens per merged PR, tokens per cycle-time hour, first-time policy pass rate. Use when asked what a skill cost, which skills are expensive, how a harness compares, or to pull numbers out of Tempo, Prometheus or the warehouse.
license: MIT
metadata:
  version: "1.0.0"
  standard_id: STD-TEL-001
  policy_ids: "telemetry.manifest_valid"
  owner: platform-observability
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: manual
---

# Query standards telemetry

Three stores, each right for a different question:

| store | reach for it when | endpoint |
|---|---|---|
| **Postgres** | anything joining skills to delivery outcomes | `postgresql://postgres:stdtel@localhost:5432/stdtel` |
| **Tempo** | one session, one span, "what happened in this run" | `http://localhost:3200` |
| **Prometheus** | rates and trends over time, pre-aggregated | `http://localhost:9090` |

## Start here

```bash
psql "$STDTEL_DSN" -c "\dt"                                    # what exists
psql "$STDTEL_DSN" -c "SELECT count(*) FROM skill_invocation;" # and how much
```

**Always report row counts alongside any number.** This dataset is usually far smaller than it
looks, and a ratio computed from three rows reads identically to one computed from three thousand.

## The primary ratios

Tokens per merged PR, by skill:

```sql
SELECT i.skill_name, count(DISTINCT p.pr_id) AS prs,
       sum(i.tail_tokens)::numeric / NULLIF(count(DISTINCT p.pr_id), 0) AS tokens_per_pr
FROM skill_invocation i
JOIN pull_request p ON p.ticket_id = i.ticket_id AND p.merged_at IS NOT NULL
WHERE i.ticket_id <> 'unattributed'
GROUP BY 1 ORDER BY tokens_per_pr DESC;
```

Tokens per cycle-time hour (cost per unit of delivery speed):

```sql
SELECT i.skill_name, sum(i.tail_tokens) / NULLIF(sum(t.cycle_time_hours), 0) AS tokens_per_hour
FROM skill_invocation i JOIN ticket t USING (ticket_id)
WHERE t.cycle_time_hours IS NOT NULL GROUP BY 1;
```

Cache-hit rate — the measure that most sharply separates the harnesses:

```sql
SELECT harness,
       sum(cache_read_tokens)::numeric
         / NULLIF(sum(cache_read_tokens + cache_creation_tokens + input_tokens), 0) AS cache_hit_rate
FROM skill_invocation GROUP BY 1;
```

Tool-call failure rate, by harness — a leading indicator of a skill telling the model to do
something the environment cannot do:

```sql
SELECT harness,
       sum(tool_failures)::numeric / NULLIF(sum(tool_calls), 0) AS failure_rate,
       sum(tool_calls) AS calls
FROM session_cost GROUP BY 1;
```

Per-tool counts live on the span as `std.session.tool.<name>.calls` / `.failures`; query them in
Tempo or Prometheus rather than Postgres, which stores the session totals.

First-time policy pass rate is the primary effectiveness metric; `warehouse/scorecard.sql` computes
it with the with/without arms already separated. Run that rather than rewriting it.

## Read it honestly

- **`tail_tokens` is not the session's whole cost.** It counts tokens after a skill loaded until the
  next one loads or the turn ends. Total spend lives in `session_cost`, which is populated for every
  session including those that loaded no skill. **Never sum the two** — invocation tail is a share of
  session total. State which denominator a ratio uses:

  ```sql
  -- total spend per merged PR (the honest cost-per-PR)
  SELECT p.pr_id, sum(sc.input_tokens + sc.output_tokens) AS total_tokens
  FROM session_cost sc JOIN pull_request p ON p.ticket_id = sc.ticket_id
  WHERE p.merged_at IS NOT NULL GROUP BY 1;

  -- how much of that total any skill was able to claim
  SELECT sc.ticket_id, sum(sc.input_tokens + sc.output_tokens) AS total,
         coalesce(sum(i.tail_tokens), 0) AS skill_attributed
  FROM session_cost sc LEFT JOIN skill_invocation i USING (session_id) GROUP BY 1;
  ```
- **`unversioned` means not in the catalogue**, not unversioned upstream. Exclude from outcome
  analysis, keep for cost, and suggest `stdtel-onboard`.
- **`unattributed` means the branch carried no ticket key.** Same rule. Report what share of rows
  this is — it bounds every delivery-joined conclusion.
- **`load_tokens` is chars/4.** An ordering signal, never a token count.
- **`harness_arm = 'mixed'`** on a ticket means its PRs disagreed; exclude it from crossover
  comparisons rather than picking one.
- **Cost is not comparable across harnesses** — Copilot bills AI Credits, Claude Code USD. Compare
  tokens, or state your conversion.

## Tempo and Prometheus

```bash
curl -s --get http://localhost:3200/api/search \
  --data-urlencode 'q={ resource.std.ticket.id = "PLAT-42" }' \
  --data-urlencode "start=$(( $(date +%s) - 86400 ))" --data-urlencode "end=$(date +%s)"
```

Tempo search **requires** `start` and `end`; without them it returns zero results and looks like an
empty pipeline. Prometheus carries `traces_span_metrics_*` dimensioned by `std_skill_name`,
`std_skill_version`, `std_skill_plugin`, `std_harness`, `std_team`.
