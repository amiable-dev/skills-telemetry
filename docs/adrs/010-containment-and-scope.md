---
title: "ADR-010: Containment and scope — what a loop skill costs, when the work spans many turns"
status: proposed
date: 2026-09-21
tags: [adr, telemetry, capture, efficiency, containment, scope]
links: ["002-delivery-data-joins-and-proxies.md", "005-data-integrity.md", "006-langfuse-as-an-optional-trace-backend.md", "009-artefact-activation-as-the-unit-of-capture.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/78"
---

## Context

[ADR-009](009-artefact-activation-as-the-unit-of-capture.md) made the artefact activation the unit of
capture, so a session's cost can be split across skills, sub-agents, compactions and turns. That
answers "what did this turn cost". It does not answer the question that prompted it: **what did this
skill cost, including everything it caused to run.**

The case that exposed the gap is a loop skill. It walks an epic ticket-by-ticket: tests, draft pull
request, review loop, a mandatory council consultation, merge, next ticket. Its own activation is a few
seconds of tool call. Everything expensive happens *underneath* it, across hundreds of turns and, in
one real session, seven weeks of wall-clock. The skill's recorded cost is therefore near zero while the
work it drives is the largest line item on the bill.

Two facts about the current implementation frame everything below.

**Activations are flat.** Both emit paths call `start_span` with no parent context, so every span is
the root of its own trace, related only by a `session.id` attribute (#75). ADR-009 asserts the
opposite. Containment is not merely unqueryable today; it is unrecorded.

**The ticket is stale.** `std.ticket.id` is derived from the git branch once per `SessionStart` and
then reused unchanged (#77). A session that changes branch without an intervening compaction stamps
every later span with the previous ticket.

A third fact bounds the solution space. A session is not a bounded unit of work. It is resumed and it
persists. The session behind this ADR ran from 2026-07-26 to 2026-09-18 with a five-week gap in the
middle. Any design whose container is the session inherits that duration.

## Options considered

**One trace per session, every activation parented to it.** This is what ADR-009's wording implies. It
is rejected on the duration fact above: a seven-week trace outgrows Tempo's per-trace limits, and spans
arriving long after the root land in fragments once the block holding it has flushed. #67 already cost
a misdiagnosis to back-dated spans; this would make that behaviour routine rather than exceptional.

**Span links.** OpenTelemetry's first-class primitive for "causally related, different trace", and the
spec-correct answer on paper. Rejected on tooling, with evidence rather than assertion. Langfuse does
not honour links at all, confirmed by a maintainer and untracked. Our own Tempo 2.7.0 does accept them
— probed directly, `{ link:traceID = … }`, `{ link:spanID = … }` and `{ link.attr = … }` all return
200, while `{ link:other = … }` is a parse error — but TraceQL evaluates one trace at a time. A link
query finds the span that *holds* a link. It never joins the linked trace into the result, which is
precisely what a cost rollup is. A primitive we cannot aggregate over is not a rollup mechanism.

**`gen_ai.conversation.id` as the scope key.** Rejected because the specification forbids exactly this
use: instrumentations "SHOULD NOT populate conversation id" as a fallback, and a new identifier, a
trace identifier or a content hash "SHOULD NOT be used". It is for real upstream conversation
identifiers only. Borrowing it would put a knowingly non-conformant value in a reserved name.

**A span tree across turns, with trace ids derived from the session id.** Each hook is a separate
process with a fresh SDK, so cross-process parentage requires deriving the trace id deterministically
rather than inheriting an ambient context. Technically possible, rejected because it buys a tree that
is still unbounded in time — it reintroduces the first option's problem with extra machinery, and a
derived-id scheme that silently mismatches produces spans that look parented without being so.

**A bounded tree inside each turn, plus a scope attribute across turns.** Chosen. It is also what the
ecosystem converged on. Traceloop builds real parentage for a workflow and then copies
`traceloop.workflow.name` onto every descendant so queries need not walk the tree. OpenInference makes
`AGENT` a container kind inside a single trace and offers nothing across traces. Langfuse groups traces
under a session identifier. Three independent implementations, one shape: real structure inside a
bounded run, a plain correlation attribute once the run stops being the right unit. No vendor has a
primitive for containment that lasts weeks; the honest reading is that this is unsolved industry-wide,
not that we are picking the lesser option.

## Decisions this draft assumes

Two questions were open when this was drafted. The draft takes a position on both so that review is a
confirmation or a reversal rather than an excavation.

1. **How a boundary is known: the hybrid.** The artefact declares which unit it works in; stdtel
   observes when that unit closed. **This is conditional on #77.** Detecting "the ticket changed"
   cannot work while the ticket only changes at `SessionStart`. If #77 is not taken, the fallback is
   declaration-only, where the scoping artefact emits an explicit close signal each iteration — more
   work for the skill, less for stdtel.
2. **Who reports spend that happens outside the harness: the spending process.** `llm-council` stamps
   its own spans using the session id the harness exports. The alternative, stdtel parsing council's
   audit trail, needs no cooperation but makes this repo depend on another system's file format.

## Decision

1. **Activations are children of their turn, not of the session.** One `Stop` process emits an entire
   slice, so a turn span with its skill, sub-agent and compaction children is a single bounded trace
   built inside one `TracerProvider`. This corrects #75 and supersedes the ADR-009 sentence amended on
   2026-09-18. Joining to the session stays what it is today: an attribute.

2. **Containment across turns is carried by attributes, not structure.** Three keys, stamped on every
   activation emitted while a scope is open:

   | attribute | meaning | bounded? |
   |---|---|---|
   | `std.scope.name` | catalogue name of the scoping artefact | yes — safe as a metrics dimension |
   | `std.scope.key` | the unit instance, normally the ticket | yes in practice — safe |
   | `std.scope.id` | unique per container instance | **no — never a metrics dimension** |

   `std.scope.key` deliberately reuses the ticket, which is already the delivery join key under
   [ADR-002](002-delivery-data-joins-and-proxies.md). The rollup is then a `GROUP BY` in the warehouse,
   which is where [ADR-006](006-langfuse-as-an-optional-trace-backend.md) already puts analysis.
   `std.scope.id` exists so that two runs of the same skill against the same ticket stay separable;
   it is unbounded by construction and is subject to the same prohibition as `std.prompt.id` (#42).

3. **Scope is declared by the artefact, detected by the harness, and may be supplied by an overlay.**
   Only the artefact knows whether it works turn-by-turn or ticket-by-ticket. Only the harness sees
   when the unit closed. For artefacts nobody here owns, the existing overlay mechanism supplies the
   declaration without editing upstream files, in the direction ADR-004's amendment already fixed:
   overlays fill gaps and never override. An artefact that declares nothing falls back to the turn and
   is still measured. **Nothing is required of any author.** How the declaration is written for each
   artefact type is [ADR-011](011-decorating-artefacts.md).

4. **A scope ends at the first of three events**: the session ends, a superseding scoped activation
   begins, or the artefact emits an explicit close. Without this, "attributable to the parent
   regardless of timeframe" means "forever", and a single July invocation owns every span since.

5. **One scope level.** Epic, ticket and turn is three levels deep and this ADR supports one. Nesting
   scopes is a known limitation, recorded below rather than half-built.

6. **Self cost is stored; inclusive cost is derived.** Every activation records what it spent directly.
   No row stores a total that includes its children. Rollups are computed at read time over
   `std.scope.*`. This is the profiler distinction between self and total time, and storing both as
   peers is how double counting becomes structural. The project already carries a rule that two span
   types must never be summed; that rule exists because people sum them.

7. **A fifth kind, `external`, for spend outside the harness.** The kind list is closed and
   test-enforced, so a new member is a schema decision. Its allowlist:

   ```
   _COMMON                          kind, source, session.id, gen_ai.usage.*
   std.artefact.name                the system, e.g. "llm-council" — bounded
   std.external.system              same value, explicit
   std.external.operation           bounded verb, e.g. "consult", "verify"
   std.external.cost_usd            what the caller actually paid
   std.external.requests            request count
   std.external.duration_ms
   gen_ai.request.model
   gen_ai.operation.name
   std.scope.*                      so external spend rolls up like everything else
   ```

   It carries no free-text and no identifiers beyond the scope keys. `std.external.cost_usd` is the
   only place in the schema where a currency amount is recorded, because it is the only place where
   the spending process knows something the harness cannot observe.

8. **`gen_ai.operation.name` is emitted as an attribute; it does not replace the kind.**
   `std.artefact.kind` remains the discriminator and the allowlist key. Alongside it, a sub-agent
   carries `invoke_agent`, a skill `execute_tool`, and a scoped skill `invoke_workflow`, so the data is
   legible to any OpenTelemetry-native tool. These conventions are at Development status in a
   repository with no tagged release, so the ADR pins the date it read them (2026-09-19) rather than a
   version that does not exist.

9. **A rollup answers containment, never causation.** "What was incurred under this skill" is what
   `std.scope.*` measures. "What this skill caused" needs the with-and-without arm, one level up, at PR
   grain. The keep / refine / merge / deprecate decision stays on the causal metric. Recording a
   containment total and reading it as an effect would reproduce the project's cardinal error one layer
   higher, and the analyst brief must say so.

## Consequences

**#77 is a precondition, not a footnote.** The hybrid boundary reads the ticket. While the ticket is
resolved once per `SessionStart`, a loop skill's iterations are invisible and every iteration inherits
the wrong key. Worse, a stale key does not fail — it joins successfully to the wrong pull request. That
is a confident wrong answer rather than missing data, which [ADR-005](005-data-integrity.md) treats as
the more serious failure.

**Double counting becomes the standing risk.** Today the overlap is one documented pair. Once
containment is pervasive, session contains turn contains skill contains sub-agent contains external
call, and every naive `SUM` over activations is wrong. Decision 6 is what keeps this manageable, and it
needs a test asserting that no stored column is an inclusive total.

**The leaf attribution does not get truer.** Containment becomes readable. The tail rule underneath it
is still a heuristic split of a turn's tokens across the skills active in it, which is why
`tail_tokens_first_only` exists as a sensitivity check. A scope rollup inherits that uncertainty and
must not be presented as measurement.

**Cardinality is bounded by construction.** `std.scope.name` and `std.scope.key` may be spanmetrics
dimensions; `std.scope.id` may not. The dashboard test that already forbids `std_prompt_id` in a
PromQL expression extends to `std_scope_id`.

**Cross-process attribution is unauthenticated.** Any process that can read the environment can claim
any session. For local, single-user telemetry this is acceptable and stated rather than discovered.
`TRACEPARENT` would have removed the problem, and was measured absent in both a main-session and a
sub-agent shell on 2026-09-19, consistent with the design proposal's note that propagation applies to
the SDK and headless runs. A sub-agent's shell reports the *parent* session id together with
`CLAUDE_CODE_CHILD_SESSION`, so external spend attributes to the session but not to a specific
sub-agent unless the agent id is passed explicitly.

### Known limitations

- **Nesting is unsupported.** An epic scope containing ticket scopes is not representable. A second
  scoped activation closes the first rather than nesting under it.
- **A scope that never closes is silently wrong.** If a session dies without a `Stop`, the last scope
  has no end. The three termination conditions bound this but do not eliminate it.
- **Declared requirements are not verified.** An artefact may declare a scope and never open it, or
  open one and do nothing inside. The data shows what ran, and divergence from what was declared is a
  finding rather than an error.

## Related

The unit of capture and the per-kind allowlists are
[ADR-009](009-artefact-activation-as-the-unit-of-capture.md), whose parentage sentence this ADR
supersedes. The ticket as the delivery join key is
[ADR-002](002-delivery-data-joins-and-proxies.md). The rule that an unobserved value is never recorded
is [ADR-005](005-data-integrity.md). Where analysis lives is
[ADR-006](006-langfuse-as-an-optional-trace-backend.md). How each artefact type declares its scope,
including sub-agents and MCP servers, is [ADR-011](011-decorating-artefacts.md).
