# Efficiency queries

ADR-009 decision 8: *the insight surface is an agent over versioned SQL, not an agent writing SQL.*
These five files are the named questions. An agent — or a person — runs them; nobody rewrites them
at the prompt, because a query that changes between two readings cannot show a change over time.

| file | question |
|---|---|
| `01_tokens_by_artefact_kind.sql` | Where did one session's tokens go, by artefact kind? |
| `02_subagent_cost_per_call.sql` | What does one call to each sub-agent type cost? |
| `03_compaction_frequency.sql` | How often does context compact, and what preceded each one? |
| `04_hook_latency_by_hook.sql` | Which hook is spending the wall time? |
| `05_skill_cache_creation_share.sql` | How much of a skill's tail is cache **creation** rather than reuse? |

Every file opens with what it answers and what it does **not** prove; read that before quoting a
number out of one. Every file returns a row count (`n_rows`, or an `n_*` column per group) so the
volume behind a figure is visible in the same output as the figure.

Parameters are `psql`-style (`:session_id`, `:since`, `:until`), as in `scorecard.sql`. Bind them:

```bash
psql "$DSN" -v session_id="'abc123'" -f warehouse/efficiency/01_tokens_by_artefact_kind.sql
```

Two rules hold across all five, and both come from how the data is produced:

- **A turn is counted with `count(DISTINCT prompt_id)`, never `count(*)`.** A turn emits one row per
  Stop carrying that slice's delta, so two Stops inside one turn produce two rows. They sum
  correctly and they must not be counted twice.
- **Kinds overlap deliberately and must never be summed together.** A skill's tail tokens are also
  inside the turn that loaded it. The same rule that governs `std.session.cost` versus
  `std.skill.invocation` governs these.

Absent measurements are NULL, not zero (ADR-005). `sum()` skips them, so each query also reports how
many rows in the group carried no usage at all — a mean over three observed rows out of two hundred
is a different claim from a mean over two hundred.
