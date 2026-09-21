# Getting insight out of the data

Worked examples against the local stack. **Every output below is real** — captured from this
repository's own dogfooding, not illustrative. The row counts are genuinely that small, which is why
half these examples end in "you cannot answer that yet". That is the point: knowing when the data
cannot answer a question is most of the value early on.

Run `make up` first, and see [local-stack.md](local-stack.md) for endpoints and credentials.

**No data of your own yet?** `make demo` loads a synthetic fleet — 120 PRs, two skills, eight
developers across six weeks — so the scorecard has something to show. Every identifier is `DEMO-` or
`demo-` prefixed and `make demo-clear` removes it. It is deliberately a mixed picture: one skill with
a clear effect, one too thinly sampled to judge, and a share of `unattributed` and `unversioned` rows,
because that is what real data looks like early on.

> The synthetic data is illustrative, not a benchmark. It was also the first thing ever to run
> `scorecard.sql` against rows, which immediately found two defects in it (issue #23) — so treat its
> `recommended_action` column as untrustworthy until that is fixed.

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

## 7. What did a loop skill really cost?

The question this repository was built for, and the one it could not answer until ADR-010. A skill that
drives work ticket by ticket has an activation lasting seconds, while the work it causes runs for
hours. Read its own row and it looks free.

```
make demo                        # or load your own
psql -f warehouse/efficiency/06_scope_self_vs_inclusive.sql \
     -v scope_name=demo-epic-loop -v since=2026-06-01
```

```
 scope_key   scope_source  n_runs  n_turns  self_tokens  inclusive_tokens
 DEMO-101    artefact           1        8       15,044           125,060
 DEMO-186    overlay            1        2            0           113,348
 DEMO-103    artefact           1        9       17,227            39,068
```

**How to read it.** `self_tokens` is what the skill's own activations carried. `inclusive_tokens` is
everything recorded while its container was open. One row per ticket, because the scope key is the
ticket and it rolls every iteration — so these are iterations of one run, not three separate runs.

The first row is the shape you are looking for: the skill accounts for about a tenth of what happened
under it. That ratio is where optimisation effort belongs, and it is invisible in any per-skill view.

**Four ways to misread it.**

*As causation.* It is not. Everything recorded while the container was open is included, including an
unrelated question typed mid-loop. Nothing can observe whether a skill caused a later tool call, so
this answers **what was incurred under** the skill. Whether the skill was worth it is still the
with-and-without arm at PR grain, and no volume of this data substitutes for it.

*By summing the two columns.* They overlap by construction: `self` is inside `inclusive`. Adding them
double-counts, the same trap as the session-cost and skill-tail pair.

*By comparing iterations as though they were equal work.* `DEMO-101` covers eight turns and `DEMO-186`
covers two. One ticket may be a typo fix and the next a migration. `n_turns` is in the output so that
this is visible rather than assumed.

*By treating an overlay row as the artefact's own claim.* `scope_source = overlay` means somebody here
asserted that unit on a third party's behalf. It can be wrong, and it can go stale when the artefact
changes without changing its name.

**A zero in `self_tokens` is not a bug.** It means the container is open but the skill did not activate
again in that window — the normal case for a loop, which activates once and then runs.

## From nothing to a populated dashboard

Two paths, both verified end to end. Run them in order: the synthetic one shows what a finding looks
like, the real one shows what your own data will actually do at first.

### Synthetic — what a finding looks like

```bash
make up && make demo
open http://localhost:3000     # "Skill scorecard"
```

```
skill                 n_with   with   n_wo  without  action
structured-logging        38   0.74     53     0.40  keep
stdtel-onboard            10   0.60     19     0.53  insufficient-data
uncatalogued-helper        -      -      -        -  insufficient-data
```

One finding and two refusals, on 120 synthetic PRs. `make demo-clear` removes it.

### Real — what yours will do

```bash
# 1. check the install before trusting anything it produces
stdtel-doctor

# 2. work on a ticket-prefixed branch, or none of it is attributable
git checkout -b feature/PLAT-42-thing

# 3. telemetry flows from your hooks; load it
python warehouse/load_traces.py --tempo http://localhost:3200 --dsn "$STDTEL_DSN" --since 24h

# 4. delivery outcomes from GitHub
python -m warehouse.load_delivery --repo owner/name --dsn "$STDTEL_DSN" --since 30d

# 5. policy results, which only CI can record (run_seq is the metric)
stdtel-policy-report --pr-id owner/name#42 --run-seq 1 --out policy.jsonl
```

Run against this repository's own history, that produces:

```
skill                n_with  n_without  action
stdtel-query              1          -  insufficient-data
structured-logging        -          1  insufficient-data
```

**That refusal is the correct output**, and it is what you should expect for weeks. One PR cannot
support a comparison; see [evaluation-power.md](evaluation-power.md).

### The fault you will actually hit

Loading this repository's first ten merged PRs produced **zero ticket rows** — none of those branches
carried a ticket key, so every one is `unattributed` and excluded from outcome analysis. The eleventh,
on `feature/STDTEL-15-doctor`, produced a ticket row immediately.

This is not recoverable later: renaming a branch tomorrow does not retroactively attribute today's PRs.
`stdtel-doctor` checks it, and the data-quality panel on the dashboard counts it.

## The recurring tells

| looks like | actually is | the tell |
|---|---|---|
| a cheap skill | not measured | `llm_requests = 0` |
| no effect | no data | join inputs are empty; print counts |
| a harness comparison | one group | `count(DISTINCT harness) = 1` |
| an empty pipeline | a missing time range | Tempo needs `start`/`end` |
| total cost per PR | skill-attributed only | using `tail_tokens` where `session_cost` was meant |
| what a skill caused | what happened while it ran | reading `inclusive_tokens` as an effect |
