# Getting insight out of the data

Worked examples against the local stack. **Every output below is real** — captured from this
repository's own dogfooding, not illustrative. The row counts are genuinely that small, which is why
half these examples end in "you cannot answer that yet". That is the point: knowing when the data
cannot answer a question is most of the value early on.

Run `make up` first, and see [local-stack.md](local-stack.md) for endpoints and credentials.

```bash
export STDTEL_DSN=postgresql://postgres:stdtel@localhost:5432/stdtel
psql "$STDTEL_DSN" -c "SELECT count(*) FROM skill_invocation;"   # always start here
```

## Making the agent available

`agents/skill-scorecard-analyst.md` ships in the plugin. Agent definitions are **loaded at session
start**, so a session already running will not see a newly added agent:

```bash
# either install the plugin
/plugin marketplace add amiable-dev/skills-telemetry
/plugin install stdtel@amiable-standards

# or, for local work, drop it in and start a new session
mkdir -p .claude/agents && cp agents/skill-scorecard-analyst.md .claude/agents/
```

Then ask in plain language — "review our skill performance and tell me what to deprecate". The agent
queries Postgres, Tempo and Prometheus over Bash. What it does *not* do is answer when the data cannot
support an answer; see example 2.

---

## 1. What did each skill cost?

Answerable at any volume. Cost is a description of what happened, not a comparison.

```sql
SELECT skill_name, count(*) AS invocations, sum(tail_tokens) AS tail_tokens,
       sum(llm_requests) AS llm_requests
FROM skill_invocation GROUP BY 1 ORDER BY 3 DESC;
```

```
     skill_name     | invocations | tail_tokens | llm_requests
--------------------+-------------+-------------+--------------
 stdtel-onboard     |           1 |       28199 |            1
 structured-logging |           1 |           0 |            0
```

**A plausible and completely wrong reading of this table: "structured-logging is free."**

It is not. `tail_tokens = 0` means no LLM request followed the skill load in that turn — the tail rule
attributes tokens from *after* a skill loads until the next one loads or the turn ends, so a skill
loaded at the very end of a turn accrues nothing. The tell is in the next column: `llm_requests = 0`.

**Never read `tail_tokens` without `llm_requests` beside it.** Zero requests means "not measured
here", not "cost nothing". Two invocations cannot rank two skills in any case.

## 2. Which skills should we deprecate?

**Correct answer: none, and the data cannot say.** This refusal is the right output, not a failure.

```sql
SELECT count(*) AS policy_rows FROM policy_result WHERE run_seq = 1;
```

```
 policy_rows
-------------
           0
```

Deprecation is an *effectiveness* judgement, and effectiveness here is first-time policy pass rate
with versus without the skill. With zero `policy_result` rows there is no outcome signal at all — not
a weak one, none. Even fully populated, 2 invocations is far below the floor: **below 30 merged PRs
per arm, or fewer than 5 developers, report descriptively and make no comparative claim.**

What to say instead: report the cost picture, name what is missing (`policy_result` needs the CI
artefact — see `warehouse/load_delivery.py --policy-results`), and state the volume that would answer
the question. See [evaluation-power.md](evaluation-power.md).

## 3. Does structured-logging improve first-time policy pass?

The scorecard query exists and is correct; it simply has nothing to work on.

```bash
psql "$STDTEL_DSN" -v week_start="'2026-09-01'" -v week_end="'2026-09-08'" -f warehouse/scorecard.sql
```

```
 skill_name | skill_version | harness | invocations | ... | first_pass_rate_with | n_with | ... | recommended_action
------------+---------------+---------+-------------+-----+----------------------+--------+-----+--------------------
(0 rows)
```

It joins `skill_invocation → ticket → pull_request → policy_result`. Three of those four are empty, so
it returns its full column list and no rows. **An empty result from a join is not evidence of no effect** — it is evidence of
no data, and the two look identical in a report. Always print the input counts alongside:

```sql
SELECT (SELECT count(*) FROM skill_invocation) AS invocations,
       (SELECT count(*) FROM pull_request)     AS prs,
       (SELECT count(*) FROM policy_result)    AS policy_results;
```

## 4. Tokens per merged PR

```sql
SELECT count(*) AS session_rows, count(p.pr_id) AS matched_prs
FROM session_cost sc LEFT JOIN pull_request p ON p.ticket_id = sc.ticket_id;
```

```
 session_rows | matched_prs
--------------+-------------
            2 |           0
```

Two sessions of real spend, joined to zero PRs. Written as a ratio with a `LEFT JOIN`, this would
divide by `NULL` and report nothing — indistinguishable at a glance from "the answer is zero".

Note also **which denominator you are using**. `session_cost` is total spend; `skill_invocation`
`tail_tokens` is the share one skill could claim. They overlap and must never be summed. Cost per PR
built from `tail_tokens` alone understates real spend by however much work happens with no skill
loaded, which is usually most of it.

## 5. How do the harnesses compare on cache-hit rate?

```sql
SELECT harness, count(*) AS sessions, sum(cache_read_tokens) AS cache_read,
       round(sum(cache_read_tokens)::numeric
             / NULLIF(sum(cache_read_tokens + cache_creation_tokens + input_tokens), 0), 3)
         AS cache_hit_rate
FROM session_cost GROUP BY 1;
```

```
   harness   | sessions | cache_read | cache_hit_rate
-------------+----------+------------+----------------
 claude-code |        2 |        200 |          0.488
```

**The second plausible-but-wrong reading, and the more dangerous one: this looks like a comparison.**

It has a `GROUP BY harness`, a rate column, and a number that looks reasonable. It compares nothing —
there is one row, because only one harness has ever reported. A `GROUP BY` returning a single group
reads as a comparison in every chart tool you will paste it into.

The tells: one row, and `sessions = 2`. **Check the number of groups before reading any grouped
result as a comparison.** And note that even with both harnesses present, cost is not comparable in
currency — Copilot bills AI Credits, Claude Code bills USD. Compare tokens, or state the conversion.

## 6. One session, in detail (Tempo)

```bash
curl -s --get http://localhost:3200/api/search \
  --data-urlencode 'q={ resource.std.ticket.id = "PLAT-99" }' \
  --data-urlencode "start=$(( $(date +%s) - 86400 ))" --data-urlencode "end=$(date +%s)"
```

**Tempo returns zero results without `start` and `end`.** No error, no warning — an empty result that
reads exactly like an empty pipeline. This cost real debugging time during development; if a Tempo
query returns nothing, check the time range before concluding anything.

## The recurring tells

| looks like | actually is | the tell |
|---|---|---|
| a cheap skill | not measured | `llm_requests = 0` |
| no effect | no data | join inputs are empty; print counts |
| a harness comparison | one group | `count(DISTINCT harness) = 1` |
| an empty pipeline | a missing time range | Tempo needs `start`/`end` |
| total cost per PR | skill-attributed only | using `tail_tokens` where `session_cost` was meant |
