---
title: "ADR-012: External contract v3 — join spend to the exact tool call, and say when a cost is partial"
status: accepted
date: 2026-09-28
tags: [adr, contract, external, mcp, joins, data-integrity]
links: ["005-data-integrity.md", "009-artefact-activation-as-the-unit-of-capture.md", "010-containment-and-scope.md", "011-decorating-artefacts.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/94"
---

## Context

[ADR-010](010-containment-and-scope.md) decision 7 defines the `external` kind: spend that a process
outside the harness reports about itself. Version 2 of that contract (#88) split the amount by
provenance. The first real emitter, llm-council, adopted it on 2026-09-28, and its first consult reached
the warehouse end to end. Running it for real exposed two gaps that no test could have found.

**The session id goes stale.** An emitter that runs as an MCP server learns the Claude session from
`CLAUDE_CODE_SESSION_ID`, which it reads once, when the harness starts it. `/clear` gives the window a
new session and does not restart the server. Observed: a council consult at 21:37:44 was stamped
`e2fe1d64`, the session from before the clear, while the window's active session was `bdd058e7`. The
spend joined to a session that had already ended. Every consult after a `/clear` in a long-lived window
is misattributed the same way. `--resume` is safe: it keeps the id.

**There is no per-call session id to read instead.** A stub stdio MCP server on Claude Code 2.1.284
logged exactly what the harness sends. `initialize` carries `clientInfo` and nothing session-shaped. A
`tools/call` carries:

```json
"_meta": {"claudecode/toolUseId": "toolu_…", "progressToken": 2}
```

That `toolUseId` is the id of the `tool_use` block in the **calling** session's transcript. Checked by
finding the probe's id there: the block is in the transcript of the session that made the call, and its
`tool_result` entry carries that turn's `promptId`. stdtel's Stop hook already reads the transcript, and
its turn pass already walks every `tool_use` block, keeping only the name for anything that is not a
Skill.

**A partially priced run has no honest shape.** A consult fans out to several models. When one reports
no cost and has no list price, the observed sum of the rest is a lower bound. Version 2 cannot say so:
`cost_usd` means "observed", with no way to add "incomplete". Council's answer in #708 was to omit
`cost_usd` for such runs, keeping only the estimate. That is correct under v2, and it discards a real
observed figure because the contract cannot qualify it.

A smaller issue came up at the same time: council's span carried
`gen_ai.request.model = anthropic/claude-opus-5` for a run of eight requests across several models. A
reader takes that to mean all eight went to one model.

## Options considered

**For the stale session id**

- **Read the session per call from MCP metadata.** Rejected, because it does not exist: verified above.
  Nothing in `initialize` or `tools/call` names the session.
- **Have the emitter find the current session itself**, for example from the newest transcript file in
  its project directory. Rejected. That is a guess presented as an observation (ADR-005). Several
  windows share a project directory, and the newest file is simply whichever window wrote last.
- **Stop sending `session.id` from MCP emitters.** Rejected. It is correct until the first `/clear` in
  a window, which covers most runs, and dropping it discards every correct join to avoid the wrong ones.
- **A new artefact kind, `tool_call`, emitted for every MCP call.** Rejected. The kind list is closed
  and each member is a schema decision (ADR-009). It would also emit a span per tool call to carry one
  id, when the turn span already exists.
- **Record the mapping in the `PostToolUse` hook.** Rejected, though its payload does carry
  `tool_use_id`, `session_id` and `prompt_id`. `post_tool_use` updates state with a plain load and
  save, not the locked `mutate()` that #74 introduced for concurrent writers. Parallel tool calls
  would lose updates, and a mapping that silently drops entries is worse than none. The transcript
  pass sees the same ids without touching state.
- **Join on `tool_use_id` from the transcript** — chosen.

**For the partial cost**

- **Keep v2 behaviour: omit `cost_usd` when incomplete.** Rejected as the permanent answer. It turns
  a partly observed run into one that looks unobserved, and the observed part is lost.
- **A boolean `cost_complete`.** Rejected in favour of a count. "Incomplete" does not distinguish one
  unpriced call in eight from seven.
- **A count of unpriced requests** — chosen.

**For the model attribute**

- **Add a list-of-models attribute.** Rejected for now. Nothing reads it, and ADR-005 treats a value
  nothing checks as a number that looks like evidence. Clarifying when the existing attribute applies
  is enough.

## Decision

1. **`std.external.tool_use_id`** (optional string). An MCP emitter sends the value it receives as
   `_meta["claudecode/toolUseId"]` on the call that produced the run. It omits the attribute when that
   value is absent: a CLI, HTTP or CI invocation, or a host other than Claude Code. It is an opaque
   identifier, so it is metadata, and it is not a spanmetrics dimension: it is unbounded.

2. **stdtel records the other half from the transcript.** The Stop hook's turn pass keeps the id of
   every `tool_use` block whose name begins `mcp__`. The turn activation carries them as
   `std.artefact.mcp_tool_use_ids`, a list of strings. The sub-agent pass that already reads sub-agent
   transcripts (ADR-009, `summarise_subagent`) does the same on the sub-agent activation, because an
   MCP call made by a sub-agent is recorded in the sub-agent's transcript, not the parent's. Checked
   2026-09-28: 13 sub-agent transcripts on one machine contain `mcp__` calls, and one sampled id
   appeared twice in its sub-agent transcript and not at all in the parent's. The loader writes one row per
   id to a new table, `mcp_tool_call (tool_use_id PRIMARY KEY, session_id, prompt_id,
   activation_span_id)`, where `activation_span_id` is the turn or sub-agent activation that carried
   the id, and `prompt_id` is the turn's own, or for a sub-agent its parent's.

   *Amended during implementation (issue #94). The attribute is `std.artefact.mcp_tool_use_ids` on
   both kinds, rather than a `std.turn.*` name on one: one key keeps the loader to one read. The table
   drops the `tool_name` column this draft proposed. Carrying it would have needed a second list on
   the span, aligned by index, and a mismatch there would silently misname every call. The external
   row already names its system and operation.*

3. **The join happens at read time, and the observed value is never overwritten.** An external row
   keeps the `session.id` it arrived with: that is what the emitter said. Queries join
   `external.tool_use_id = mcp_tool_call.tool_use_id` and, where it resolves, attribute the spend to the
   resolved session and turn. Joining at read time means arrival order does not matter. An external span
   that lands before the turn's Stop resolves as soon as the turn loads.

   This also gives external spend a scope. The resolved turn carries `std.scope.*` from ADR-010, so
   council spend made inside an epic loop rolls up into that loop's inclusive cost, which v2 could not
   do. The emitter still never sets scope itself.

4. **`std.external.requests_unpriced`** (optional integer). This is the number of requests in the run
   with neither an observed nor an estimated cost. When it is greater than zero, `cost_usd`, if sent,
   is a **lower bound**, and the emitter sends it rather than omitting it. Query 7 counts such runs as
   `runs_partial` and excludes them from `coverage_pct`: a lower bound does not reconcile to an invoice.
   Absent means the emitter did not say, which is every v2 span. Those keep their v2 reading.

5. **`gen_ai.request.model` names the model only when every request in the run used it.** A multi-model
   run omits it. This is a clarification: no attribute is added.

6. **`session.id` from an MCP server means "the session that started this server".** This is written
   into the contract and the instrumentation skill, so nothing downstream treats it as exact. Where
   decision 1's join resolves, the resolved session wins in every query.

7. **This is contract version 3**, additive over v2. `CONTRACT_VERSION` becomes 3. The published set
   in `tests/test_external_kind.py` and the loader's own copy are extended. The change is announced to
   llm-council before merge, as ADR-010 decision 7 requires.

## Consequences

- Once an emitter adopts it, external spend is attributed to the turn that caused it. That makes
  "which turn spent money outside the harness" answerable, and scope rollups include external spend.
- A `/clear` no longer misattributes anything the join covers. What it does not cover — a run with no
  `tool_use_id`, or one whose turn never loads — falls back to `session.id`, and a query can count how
  often that happens.
- A partially priced run keeps its observed figure, labelled as a lower bound, instead of losing it.
- The turn span grows one list attribute, one entry per MCP call in the turn. It is not a metric
  dimension, so it adds no series.
- Adoption needs work in both repositories. stdtel needs decision 2, the table, the loader copy of the
  allowlist, query 7 and the skill. Council needs decisions 1, 4 and 5.

### Known limitations

- **`_meta["claudecode/toolUseId"]` is undocumented.** It was observed on Claude Code 2.1.284, the same
  kind of tolerance ADR-011 records for sub-agent front matter, and it could disappear without notice.
  The join then fails open: rows fall back to `session.id`, and the share of external rows whose
  `tool_use_id` resolves becomes the signal. Implementing this ADR adds that share to query 7, so a
  regression is visible rather than silent.
- **A tool call that never reaches a transcript cannot be joined.** A session killed before its Stop
  hook runs, or one whose transcript is unreadable, leaves its external rows on the fallback.
- **Other hosts are not covered.** An MCP server used from Copilot or another client receives no such
  id. Its spans carry `session.id` or nothing, as today.
- **Estimated and observed spend still cannot be summed into one honest total**, and this ADR does not
  try to. It only lets a partial observation be kept instead of discarded.

## Related

- [ADR-005](005-data-integrity.md): never record an unobserved value. A stale session id is an
  observation from the wrong moment, which is why the fix is a better observation rather than a guess.
- [ADR-009](009-artefact-activation-as-the-unit-of-capture.md): the closed kind list that rules out a
  `tool_call` kind.
- [ADR-010](010-containment-and-scope.md): decision 7 (the external kind and its change protocol), and
  the scope attributes that the join lets external spend inherit.
- amiable-dev/llm-council#707 (the staleness check) and #708 (the completeness question).
