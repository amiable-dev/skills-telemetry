---
name: stdtel-query
description: Query standards-telemetry data to answer questions about skill cost, token usage, cache-hit rate, and delivery outcomes — tokens per merged PR, tokens per cycle-time hour, first-time policy pass rate. Use when asked what a skill cost, which skills are expensive, how a harness compares, or to pull numbers out of Tempo, Prometheus or the warehouse.
license: MIT
metadata:
  version: "1.1.0"
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

**The hard floor: below 30 merged PRs per arm, or fewer than 5 developers, report descriptively and make no comparative claim.** Cost and usage figures are reportable at any volume; comparisons between
skills, harnesses or arms are not. See [`docs/evaluation-power.md`](../../docs/evaluation-power.md).

## The primary ratios

Tokens per merged PR, by skill:

Work reaches a change request (a PR, or a GitLab MR) through `activation_change_request`, the one
place attribution is defined ([ADR-013](../../docs/adrs/013-join-work-to-change-requests.md)). Its
`method` says how: `branch`, `branch+commit` (confirmed by a commit patch-id) or `commit`. Use it;
never join on anything else.

```sql
SELECT i.skill_name, count(DISTINCT c.cr_id) AS prs,
       sum(i.tail_tokens)::numeric / NULLIF(count(DISTINCT c.cr_id), 0) AS tokens_per_pr
FROM skill_invocation i
JOIN activation_change_request v ON v.span_id = i.span_id
JOIN change_request c ON c.cr_id = v.cr_id AND c.state = 'merged'
GROUP BY 1 ORDER BY tokens_per_pr DESC;
```

Tokens per cycle-time hour (cost per unit of delivery speed):

```sql
SELECT i.skill_name, sum(i.tail_tokens) / NULLIF(sum(t.cycle_time_hours), 0) AS tokens_per_hour
FROM skill_invocation i
JOIN activation_change_request v ON v.span_id = i.span_id
JOIN change_request_ticket ct ON ct.cr_id = v.cr_id
JOIN ticket t ON t.ticket_id = ct.ticket_id
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
something the environment cannot do — is **not in Postgres**. The per-tool counts live only on the
session span, as `std.session.tool.<name>.calls` and `.failures`: query them in Tempo, or in
Prometheus through spanmetrics. (This section used to give SQL against `session_cost.tool_failures`,
a column that has never existed; it failed for anyone who ran it.)

First-time policy pass rate is the primary effectiveness metric; `warehouse/scorecard.sql` computes
it with the with/without arms already separated. Run that rather than rewriting it.

## Read it honestly

- **`tail_tokens` is not the session's whole cost.** It counts tokens after a skill loaded until the
  next one loads or the turn ends. Total spend lives in `session_cost`, which is populated for every
  session including those that loaded no skill. **Never sum the two** — invocation tail is a share of
  session total. State which denominator a ratio uses:

  ```sql
  -- total spend per merged PR (the honest cost-per-PR): turn tokens, which are
  -- per-turn deltas, attributed to the change request each turn's work became
  SELECT c.cr_id, sum(coalesce(a.input_tokens, 0) + coalesce(a.output_tokens, 0)) AS total_tokens
  FROM artefact_activation a
  JOIN activation_change_request v ON v.span_id = a.span_id
  JOIN change_request c ON c.cr_id = v.cr_id AND c.state = 'merged'
  WHERE a.kind = 'turn' GROUP BY 1;

  -- how much of each session's total any skill was able to claim
  SELECT sc.session_id, sum(sc.input_tokens + sc.output_tokens) AS total,
         coalesce(sum(i.tail_tokens), 0) AS skill_attributed
  FROM session_cost sc LEFT JOIN skill_invocation i USING (session_id) GROUP BY 1;
  ```
- **`unversioned` means not in the catalogue**, not unversioned upstream. Exclude from outcome
  analysis, keep for cost, and suggest `stdtel-onboard`.
- **Work that joined no change request is unattributed.** Any branch name joins, so this means no
  branch identity (no git repository or no remote), work committed straight to the default branch,
  or a change request not yet loaded. Keep it for cost, exclude it from outcome, and report its share —
  it bounds every delivery-joined conclusion.
- **A `branch`-only link is not evidence the skill contributed.** The scorecard counts only
  `branch+commit` and `commit` in the with-arm, and keeps `branch`-only change requests out of *both*
  arms. Do the same in any comparison you write.
- **`load_tokens` is chars/4.** An ordering signal, never a token count.
- **`harness_arm = 'mixed'`** on a ticket means its PRs disagreed; exclude it from crossover
  comparisons rather than picking one.
- **Cost is not comparable across harnesses** — Copilot bills AI Credits, Claude Code USD. Compare
  tokens, or state your conversion.

## Tempo and Prometheus

```bash
curl -s --get http://localhost:3200/api/search \
  --data-urlencode 'q={ span.session.id = "<session id>" }' \
  --data-urlencode "start=$(( $(date +%s) - 86400 ))" --data-urlencode "end=$(date +%s)"
```

Tempo search **requires** `start` and `end`; without them it returns zero results and looks like an
empty pipeline. Prometheus carries `traces_span_metrics_*` dimensioned by `std_skill_name`,
`std_skill_version`, `std_skill_plugin`, `std_harness`, `std_team`.
