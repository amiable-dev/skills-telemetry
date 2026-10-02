---
name: stdtel-query
description: Query standards-telemetry data to answer questions about skill cost, token usage, cache-hit rate, and delivery outcomes — tokens per merged PR, tokens per cycle-time hour, first-time policy pass rate. Use when asked what a skill cost, which skills are expensive, how a harness compares, or to pull numbers out of Tempo, Prometheus or the warehouse.
license: MIT
metadata:
  version: "1.2.0"
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

A skill's cost is **the harness's own model requests named for it**: Claude Code records the skill on
every request it served ([ADR-014](../../docs/adrs/014-harness-native-telemetry.md)). Read it from
the views, never from `skill_invocation`, which records that a skill ran, not what it cost:

| view | grain | use it for |
|---|---|---|
| `skill_activation_cost` | (session, prompt, skill, version) | per-skill cost and abandonment; joins to the version stdtel recorded |
| `skill_request_cost` | (session, prompt, skill) | per-skill spend regardless of version |
| `llm_request_attributed` | one model request | anything finer; `attribution_source` says `native` or `derived` |

Work reaches a change request (a PR, or a GitLab MR) through `activation_change_request`, the one
place attribution is defined ([ADR-013](../../docs/adrs/013-join-work-to-change-requests.md)). Its
`method` says how: `branch`, `branch+commit` (confirmed by a commit patch-id) or `commit`. Use it;
never join on anything else.

Tokens and cost per merged PR, by skill:

```sql
WITH linked AS (   -- DISTINCT: a skill run twice in one prompt is one cost row, not two
  SELECT DISTINCT c.session_id, c.prompt_id, c.skill_name, v.cr_id, c.cost_usd,
         c.input_tokens + c.output_tokens + c.cache_read_tokens + c.cache_creation_tokens AS tokens
  FROM skill_activation_cost c
  JOIN artefact_activation a ON a.kind = 'skill' AND a.session_id = c.session_id
                            AND a.prompt_id = c.prompt_id AND a.name = c.skill_name
  JOIN activation_change_request v ON v.span_id = a.span_id
  JOIN change_request cr ON cr.cr_id = v.cr_id AND cr.state = 'merged'
)
SELECT skill_name, count(DISTINCT cr_id) AS prs,
       sum(tokens)::numeric / NULLIF(count(DISTINCT cr_id), 0) AS tokens_per_pr,
       sum(cost_usd) / NULLIF(count(DISTINCT cr_id), 0)       AS usd_per_pr
FROM linked GROUP BY 1 ORDER BY usd_per_pr DESC NULLS LAST;
```

Cache-hit rate, the measure that most sharply separates the harnesses:

```sql
SELECT harness, count(*) AS requests,
       sum(cache_read_tokens)::numeric
         / NULLIF(sum(cache_read_tokens + cache_creation_tokens + input_tokens), 0) AS cache_hit_rate
FROM llm_request GROUP BY 1;
```

Tool-call failure rate, by harness — a leading indicator of a skill telling the model to do
something the environment cannot do — is **not in Postgres**. The per-tool counts live only on the
session span, as `std.session.tool.<name>.calls` and `.failures`: query them in Tempo, or in
Prometheus through spanmetrics. (This section used to give SQL against `session_cost.tool_failures`,
a column that has never existed; it failed for anyone who ran it.)

First-time policy pass rate is the primary effectiveness metric; `warehouse/scorecard.sql` computes
it with the with/without arms already separated. Run that rather than rewriting it.

## Read it honestly

- **A NULL cost is unmeasured, never free.** A prompt with no row in `llm_request` came from a
  session with Claude Code's own telemetry off. `skill_activation_cost.abandoned` is NULL there, and
  the scorecard counts such activations as `unmeasured_activations` rather than averaging them in.
  Report how many there are.
- **`session_cost` is the harness's own session total, kept to check against.** Never add it to the
  per-request figures. `session_cost_reconciliation` puts the two side by side, and a large
  `unexplained_usd` means requests are missing:

  ```sql
  -- total spend per merged PR (the honest cost-per-PR): every request in a turn
  -- whose work became the change request, each counted once
  SELECT v.cr_id, count(*) AS requests, sum(r.cost_usd) AS usd
  FROM llm_request_change_request v
  JOIN llm_request r USING (harness, request_id)
  JOIN change_request c ON c.cr_id = v.cr_id AND c.state = 'merged'
  GROUP BY 1;

  -- how much of each session's spend any skill was named on
  SELECT session_id, sum(cost_usd) AS usd,
         sum(cost_usd) FILTER (WHERE skill_name IS NOT NULL AND skill_name <> 'third-party') AS skill_usd
  FROM llm_request_attributed GROUP BY 1;
  ```
- **`derived` is an attribution, `native` is a measurement.** A request Claude Code sent as
  `"third-party"` is named from the one skill stdtel saw run in that prompt, and marked
  `attribution_source = 'derived'`. One still reading `"third-party"` could not be named. Say which
  you included.
- **`unversioned` means not in the catalogue**, not unversioned upstream. Exclude from outcome
  analysis, keep for cost, and suggest `stdtel-onboard`.
- **Work that joined no change request is unattributed.** Any branch name joins, so this means no
  branch identity (no git repository or no remote), work committed straight to the default branch,
  or a change request not yet loaded. Keep it for cost, exclude it from outcome, and report its share —
  it bounds every delivery-joined conclusion.
- **A `branch`-only link is not evidence the skill contributed.** The scorecard counts only
  `branch+commit` and `commit` in the with-arm, and keeps `branch`-only change requests out of *both*
  arms. Do the same in any comparison you write.
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
