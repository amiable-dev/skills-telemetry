---
title: "ADR-009: Artefact activation as the unit of capture — sub-agents, compaction and per-turn cost beside skills"
status: proposed
date: 2026-09-15
tags: [adr, telemetry, capture, efficiency, subagents]
links: ["001-distribution-and-capture-surface.md", "003-hook-execution-constraints.md", "005-data-integrity.md", "006-langfuse-as-an-optional-trace-backend.md", "../evaluation-power.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/63"
---

## Context

The project's aim is a data-driven loop for optimising the artefacts an agent runs under. The design
proposal names one such artefact, the skill, and one question about it: first-time policy pass rate
on the PR, with the skill versus without. Everything downstream — the span schema, the warehouse,
`scorecard.sql`, both dashboards, the analyst agent — is built for that question and that artefact.

That is the right question for **effectiveness**. It is answered at PR grain, needs 30 merged PRs per
arm before it can say anything, and sees only the skill. It is the wrong question for **efficiency**:
where a session's tokens and time went, and which artefact to change to spend less for the same
outcome. Efficiency is answerable per session, at any volume, and it needs artefacts that today have
no representation at all.

The evidence came from reading this repository's own dogfooding session (2026-09-10 to 09-15)
straight out of the transcript the Stop hook already opens:

| what the session did | what stdtel recorded |
|---|---|
| `totalCostUSD` 366, split across Opus and Haiku by `modelUsage` | session totals only, and not loaded into the warehouse after 09-13 |
| 42 sub-agent transcripts under `subagents/`; one research agent alone read 3.2M cached tokens | nothing — `SubagentStop` is not a hook we install |
| one compaction dropping 954k tokens (`compactMetadata`) | nothing |
| every hook that fired, with `durationMs` (`hookInfos`) | nothing |
| 893 Bash calls, 14 web fetches, 4 `Agent` spawns | per-tool **counts** on `std.session.cost`; no cost, no duration |
| 0 Skill loads | the only artefact the schema models |

Real `skill_invocation` rows carrying tokens in the warehouse: **one**. The scorecard reports
`insufficient-data` for every row, which is the correct answer to its question — and no answer at all
to the question the person reading the dashboard was asking.

Two further facts shape the decision. First, the harness now exposes the missing artefacts as hook
events: `SubagentStop` carries `agent_id`, `agent_type` and `agent_transcript_path`; `PostCompact`
carries `compaction_reason` and before/after token estimates (Claude Code ≥ 2.1.268). Second, the
warehouse was two days behind Tempo because `load_traces.py` is run by hand — the same failure shape
as the `policy-results` artefact that nothing ingests (#21). A capture change that lands in a
warehouse nobody loads changes nothing.

## Options considered

- **Build a better UI on the existing data.** Rejected. The data does not contain the artefacts the
  UI would need to show. A different chart over one real skill row and no sub-agent rows is the same
  empty dashboard with nicer typography.
- **Replace the primary metric with cost.** Rejected. Cost is the constraint, not the objective; a
  skill that is expensive and lifts first-time pass rate is working. The with/without comparison and
  the 30-PR floor stay exactly as they are. This ADR adds a second lens beside them, it does not
  swap one for the other.
- **Rely on Claude Code's native `claude_code.*` telemetry for everything but skills.** Partly
  rejected. Native metrics carry tokens by model and cost per session, but not which sub-agent type
  spent them, not the compaction event, not the hook latency, and not the join to ticket, repo and
  skill contract that `std.*` carries. The open ADR-001 question (consume native token usage instead
  of chars/4) is compatible with this decision and still worth doing; it does not replace it.
- **One span per tool call.** Rejected, as it was in `SessionState`: at ~30 tool calls per prompt
  the volume multiplies for a metric that needs counts. Tool calls stay aggregated per session, with
  the addition of duration. Sub-agents, compactions and turns are rarer and each is a decision a
  developer can act on; those get spans.
- **A new span name per artefact kind (`std.subagent.run`, `std.compaction`, ...).** Rejected in
  favour of one span name with a `kind` attribute, narrowly. The case for separate names is real
  and was put by the council review: the kinds have different lifecycles, a union schema with many
  optional fields validates weakly, and OTel conventions prefer a name that says what the span is.
  The case for one name won because the storage and query contract is what matters: one loader,
  one table, one spanmetrics configuration, and "where did the tokens go, by artefact" as a
  `GROUP BY kind`. The cost is paid in a **discriminated schema**: each kind has its own allowlist
  of attributes, a span carrying an attribute outside its kind's list is a test failure, and a
  kind without its required attributes is not emitted.
- **Emit sub-agent cost from the parent's Stop hook by reading `subagents/*.jsonl`.** Rejected as
  the primary path. It works (the files are there and carry `usage`), but `SubagentStop` fires with
  the transcript path and the agent type in the payload, which is an observation rather than an
  inference. Reading the directory stays as the fallback for harnesses or versions without the hook,
  and is flagged `std.artefact.source = transcript` when used, per ADR-005.

## Decision

1. **The unit of capture is an artefact activation.** A new span, `std.artefact.activation`, with
   `std.artefact.kind` ∈ {`skill`, `subagent`, `compaction`, `turn`} and, for the first three, a
   bounded `std.artefact.name` (the skill's catalogue name, the `agent_type`, the
   `compaction_reason`). A turn carries **no name**: its identity is `std.prompt.id`, which is
   unbounded and must never become a spanmetrics dimension. Every activation is a child of the
   session and carries the session's resource attributes, so ticket, repo, team and harness join as
   they do now. The schema is discriminated by kind: a per-kind allowlist of attributes is the
   whole contract, enforced by the same contract test that already ties loader columns,
   `parse_span` and the schema together.
2. **Skills are one kind, not a special case, and there is no dual emission.** The wire-level span
   `std.skill.invocation` is replaced by `std.artefact.activation{kind=skill}` in one release;
   emitting both would double every `traces_span_metrics_calls_total` series and both dashboards,
   which filter on `span_name`. Compatibility is kept one layer down: the loader goes on writing
   the `skill_invocation` table from kind `skill`, so every existing query, `scorecard.sql`, and
   every row recorded before this ADR keep working unchanged. The spanmetrics dimensions and the
   dashboard `span_name` filters change in the same PR as the emitter, and the CHANGELOG names the
   rename. The contract fields (`std.standard_id`, `std.policy.ids`, `std.skill.version`,
   `content_hash`) attach to kind `skill` only. The tail rule, `tail_tokens_first_only`, and
   `telemetry.emit: false` are unchanged. The primary metric reads kind `skill` and nothing else.
3. **Sub-agents get a span each.** Install `SubagentStart`/`SubagentStop`. The span carries
   `agent_type`, `agent_id`, the model or models used, token usage summed from the sub-agent
   transcript, tool-call counts, duration, and `std.artefact.parent_prompt_id`. The payload also
   carries `last_assistant_message` and `agent_transcript_path`; **neither is ever emitted**.
   Attributes are built by picking named scalars out of the payload, as `stop()` does today; the
   raw payload is never serialised, logged, stored in state, or passed to `scrub()`, which stays as
   the second guard rather than the first. The sub-agent transcript is read with the existing
   `read_slice`, which extracts `usage` blocks and `tool_use` names and never retains message text.
   A regression test feeds content into every documented field and one undocumented field and
   asserts that none of it reaches the exported span.
4. **Compaction gets a span each.** Install `PostCompact`. The span carries `compaction_reason`,
   `token_count_estimate_before` and `_after`, and the number of turns since the previous compaction.
   Where the estimates are absent (older harness) the attributes are omitted, never zero.
5. **Turns get a span each, incrementally.** At Stop, one activation of kind `turn` per `prompt_id`
   observed in the transcript slice since the previous Stop, which is one turn in the ordinary case,
   so there is no batch to parse. Usage is summed from the slice's per-request `usage` blocks, which
   are deltas by construction; `durationMs` is copied where the harness recorded it and omitted
   otherwise. A turn with no observed `prompt_id` is not emitted. The dedupe key is
   (`session.id`, `std.prompt.id`), so a repeated Stop cannot produce a second row. The turn is the
   join key to native `claude_code.*` metrics and the denominator for per-turn ratios; it is not the
   denominator for skill effectiveness, which stays the PR.
6. **Hook latency is recorded on the turn**, as `std.turn.hook_ms` summed from `hookInfos`, with the
   per-hook breakdown as `std.turn.hook.<basename>.ms`. The hook command path is a basename only;
   full paths are local filesystem layout and stay out of the data.
7. **The loaders run on a schedule, not by hand.** A `loader` service in the compose stack (or
   `mise run load` under cron) runs `load_traces.py` and `load_delivery.py` at a fixed interval and
   writes a `loader_run` row with counts. A warehouse that lags Tempo by more than one interval is a
   doctor finding, not a surprise.
8. **The insight surface is an agent over versioned SQL, not an agent writing SQL.** The named
   efficiency questions ship as files under `warehouse/` beside `scorecard.sql`, each with a test
   over seeded rows: tokens by artefact kind for a session; cost per sub-agent type per call;
   compaction frequency and what preceded it; hook latency by hook; cache-creation share of a
   skill's tail. `skill-scorecard-analyst` gains a session-efficiency brief that runs those files,
   reports row counts and the data timestamp first, and is told, as it already is for the primary
   metric, to run the canonical query rather than rewrite it. Grafana keeps the monitoring role with
   two added panels, tokens by artefact kind and loader freshness, and Langfuse's session tree
   shows the sub-agent hierarchy without further work once activations are children of the session
   (ADR-006). No bespoke UI is built until the questions have stopped changing.

## Consequences

- The schema gains one table, `artefact_activation` (kind, name, session, prompt_id, timings,
  tokens by type, model, duration, source). `skill_invocation` stays a table the loader writes from
  kind `skill`; making it a view is a later, separate decision. The loader contract test extends to
  the new columns and to the per-kind allowlists.
- Spanmetrics gains `std.artefact.kind` and `std.artefact.name` as dimensions. Cardinality is bounded
  because a turn has no name: kind is a four-value enum, and name is a catalogue name, an agent type
  or a compaction reason. `std.prompt.id` is never a dimension.
- The span rename is a wire-level break for anyone querying Tempo by `std.skill.invocation`
  directly. It is announced in the CHANGELOG and the dashboards move in the same release.
- Hook count per session rises by two events (`SubagentStop`, `PostCompact`). Both are rare relative
  to `PostToolUse`, which is already unmatched, and both must respect the same budget: read the
  payload, append to state, exit 0. Token summing over a sub-agent transcript happens at Stop, or in
  the spool drainer once ADR-008 lands, not in the sub-agent hook.
- Copilot has no confirmed equivalent for sub-agents or compaction. Those kinds are Claude Code only
  until a surface is verified against a live trace, and the analyst brief says so.
- The dashboards will show something at any volume: one session is enough for "where did it go".
  That is the intended change. The with/without comparison is exactly as gated as before.

### Known limitations

- **Sub-agent token sums come from the sub-agent transcript's `usage` blocks.** Their cache-read
  figures are large by construction (every request re-reads the parent context); comparing a
  sub-agent's cache-read tokens with a skill's tail is comparing two different things. The analyst
  brief reports cache-read separately from the rest for that reason.
- **A sub-agent that is still running at Stop has no span yet.** `SubagentStop` fires when it ends;
  background agents end after the turn. The span is emitted at the next Stop, so a session's last
  turn can under-count until the following one.
- **`token_count_estimate_*` are estimates**, and the harness says so in the field name. They are
  recorded as received and never adjusted.
- **The Stop hook does more work per turn.** One extra span in the ordinary case, plus a sub-agent
  transcript read for each agent that finished since the last Stop. The budget in ADR-003 applies
  unchanged; the P99 on a session with many sub-agents is measured before acceptance, below.
- **This does not tell you what to change.** It tells you which artefact spent what. Deciding that a
  sub-agent should have been a skill, or that a hook is worth its latency, still needs a person or an
  agent reading the numbers against the outcome — which is what the brief is for.

## Council review

The draft was put to an LLM council (two of four models responded; no synthesis was produced).
Accepted from it: no dual emission, because it double-counts the spanmetrics series; a per-kind
allowlist rather than a denylist as the privacy control, with the raw payload never serialised;
no name on turn spans, because `prompt_id` as a dimension is unbounded; the efficiency questions as
versioned, tested SQL that the agent runs rather than writes; and most of the verification items
below. Not accepted: per-kind span names, for the reason recorded under options; and "do not read
the transcript at all", because the tail rule already depends on the existing metadata-only reader
and the sub-agent read uses the same one.

## To verify before acceptance

Per ADR-005, none of the following is assumed; each is checked against a live session and the
payload committed to `tests/fixtures/hook_payloads.json`:

- the `SubagentStop` payload fields as documented, in particular `agent_transcript_path` resolving
  to `subagents/<agent_id>.jsonl` under the session directory;
- which transcript entry carries `totalCostUSD` and `modelUsage`, and whether it is per turn or
  cumulative (it was observed once in the session that motivated this ADR, on an entry that also
  carried `durationMs`);
- that `PostCompact` fires for both `auto` and `manual`, and the estimate fields are present on the
  installed harness version; and that when they are absent the attributes are omitted, not zero;
- that the sub-agent transcript is fully flushed when `SubagentStop` fires, and whether the hook
  fires at all for a sub-agent that was interrupted or killed;
- that `usage` blocks in a sub-agent transcript are per request, as in the parent transcript, and
  not cumulative, so summing them cannot over-count;
- that `agent_id` is unique across sessions, or must be joined with `session.id`;
- that compaction appends to the parent transcript rather than rewriting it, so `transcript_offset`
  survives a compaction and no turn is read twice;
- the measured Stop-hook wall time on a session with dozens of finished sub-agents and a large
  transcript, against the ADR-003 budget;
- a leak test: content placed in every documented payload field and one undocumented field, and
  in a sub-agent transcript's message bodies, with the exported spans asserted free of all of it;
- a before/after run of `scorecard.sql` over the demo fleet proving the primary metric's cohorts,
  denominators and verdicts are byte-identical once the loader writes `skill_invocation` from
  kind `skill`.

## Related

The distribution and capture surface is [ADR-001](001-distribution-and-capture-surface.md); the
hook execution budget is [ADR-003](003-hook-execution-constraints.md); the rule that absent data is
omitted rather than zeroed is [ADR-005](005-data-integrity.md); the reason Langfuse's session tree
benefits without extra work is [ADR-006](006-langfuse-as-an-optional-trace-backend.md). The sample-size
floor that this ADR does not touch is in [`../evaluation-power.md`](../evaluation-power.md).
