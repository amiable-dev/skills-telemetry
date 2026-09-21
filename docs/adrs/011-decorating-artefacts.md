---
title: "ADR-011: Decorating artefacts — how a skill, sub-agent or MCP server declares itself"
status: proposed
date: 2026-09-21
tags: [adr, contract, onboarding, subagents, mcp, documentation]
links: ["004-skill-identity-and-catalogue.md", "005-data-integrity.md", "010-containment-and-scope.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/78"
---

## Context

[ADR-010](010-containment-and-scope.md) needs one thing from an artefact that the harness cannot
observe: which unit of work it runs in. A loop skill works ticket-by-ticket; a formatting skill works
turn-by-turn; nothing in a hook payload distinguishes them. Everything else in the containment model is
observed.

That single declaration raises a question this project has answered once before and should answer the
same way: **what do we require of people who write artefacts?** The scar is
[ADR-004](004-skill-identity-and-catalogue.md)'s amendment. The manifest gate originally demanded a
non-empty `policy_ids` from every skill. Authors who had no deterministic check cited an unrelated
policy to get past the gate, which is worse than declaring nothing, because the scorecard then scored
the skill against a rule it had nothing to do with and the number looked like evidence. The rule was
narrowed to bind a pair, and the honest answer — "nothing verifies this" — became sayable.

The three artefact types differ sharply in what can be decorated at all.

**Skills** already have a contract. Agent Skills permits exactly six top-level front-matter keys, so
every field lives under `metadata:` as strings, and `stdtel-onboard` already writes it.

**Sub-agents** have front-matter (`name`, `description`, `tools`, `model`) but no contract here, and no
`metadata:` block — not even on this repo's own analyst agent. Whether the agent loader tolerates an
unknown top-level key is **not established**. Skills hard-error on extra keys; assuming sub-agents
behave the same way, or assuming they do not, would be exactly the kind of unverified claim that put a
false sentence into ADR-009.

**MCP servers** have no file this project could decorate. Configuration is a server entry in settings,
the tools arrive as ordinary tool calls named `mcp__<server>__<tool>`, and the author of a third-party
server has no reason to know this project exists.

## Options considered

**Require a scope declaration from every artefact.** Rejected, and this project has already paid for
the lesson once. ADR-004's original gate demanded `policy_ids` from every skill, and authors with
nothing to cite named an unrelated policy to get past it, which produced numbers that looked like
evidence. A required `telemetry.scope` would do the same thing in a subtler way: the honest default is
"turn", and a gate would push authors to claim a wider scope than they run in, inflating every rollup
attributed to them.

**Invent a manifest file for MCP servers.** Rejected. Nothing would read it but us, no third-party
author would ship one, and it would exist mainly to make the contract look uniform. An artefact we
observe but do not own is a real category, and the overlay already serves it.

**Declare scope only in stdtel's own configuration, with no artefact-side field at all.** Rejected as
the sole mechanism, because the artefact is the one thing that knows its own unit of work, and a
project-local list goes stale the moment a skill is updated. Retained as the *fallback*, which is what
the overlay is.

**Edit third-party artefacts in place to add the block.** Rejected: the edit survives until the next
upstream release, plugin reinstall, or new machine, and it fails silently because the artefact keeps
emitting spans that simply arrive undecorated again. This is the reasoning ADR-004's overlay amendment
already recorded.

**A second onboarding skill alongside `stdtel-onboard`.** Rejected. Decorating a sub-agent is the same
job as decorating a skill against more targets, and a sibling would split one workflow across two
things a user must know to look for.

## Decision

1. **Nothing is required of any author.** A declaration is read when offered and defaulted when not.
   An artefact that declares no scope is scoped to the turn and is still measured. There is no gate, no
   validation failure, and no degraded status for artefacts that never opt in. ADR-010 is an
   observation system with one optional hint, and this ADR is the reason that framing holds.

2. **Skills declare scope in the existing block**: `telemetry.scope: turn | ticket | session`,
   defaulting to `turn`. This is one more string in a metadata block onboarded skills already carry,
   and it extends [ADR-004](004-skill-identity-and-catalogue.md)'s contract rather than opening a
   second one.

3. **Sub-agents carry the same `metadata:` block as skills, with a recorded caveat.** Verified
   2026-09-21 against Claude Code 2.1.277 and the sub-agent documentation: `metadata` is **not** among
   the documented agent front-matter keys, the documented skip conditions do not include unknown keys,
   and an agent file carrying a `metadata:` block passes `claude plugin validate` and loads. So this
   works, but it works by tolerance rather than by contract, which is the opposite of the skill case
   where `metadata` is explicitly specified and *other* keys hard-error on upload.

   That asymmetry is recorded rather than smoothed over. We rely on undocumented behaviour here, it
   could regress in any release, and the failure would be silent — sub-agents would simply go back to
   arriving undecorated. Decision 5 is the mitigation: the overlay carries the same declaration, so a
   regression costs a configuration change rather than a redesign. A test should assert that a decorated
   sub-agent still loads, so the day the tolerance ends is the day a test fails rather than the day the
   data quietly thins out.

4. **MCP servers are overlay-only**, keyed on the server name parsed from the `mcp__<server>__<tool>`
   tool name. Verified the same day: `.mcp.json` has no free-form metadata field, tool-level `_meta` is
   declared by the server rather than the user, and the hook payload for an MCP tool call does not carry
   it in any case. There is no file to decorate and no author to ask. This is not a limitation to be fixed
   later; it is the correct shape for an artefact we observe but do not own.

5. **The overlay is the universal fallback**, and it is what makes decision 1 affordable. Any artefact
   of any type can be described in an overlay root the operator controls, without editing upstream
   files that the next release would overwrite. The direction ADR-004 already fixed holds unchanged:
   an overlay fills gaps and never overrides, so an artefact that later declares its own scope wins
   over the local guess, and the guess becomes dead weight rather than a silent conflict.

6. **`stdtel-onboard` is extended, not duplicated.** It already decorates a `SKILL.md`; the same job
   over more targets is the same skill. A sibling would split one workflow across two things a user has
   to know about, and would need its own entry in `docs/skills.md` to satisfy the coverage test. The
   extended skill decorates a skill in place, decorates a sub-agent if decision 3 permits it, and
   writes an overlay entry when the artefact cannot or should not be edited — including the case where
   the artefact belongs to someone else.

7. **Scope of work includes the user-facing documentation, and it is not optional.** A contract nobody
   can find is a contract nobody meets. Specifically: `docs/for-developers.md` gains what
   `std.scope.*` records and how to opt out; `docs/reference.md` gains the `telemetry.scope` field and
   the overlay shape for sub-agents and MCP servers; `docs/skills.md` is updated for the widened
   `stdtel-onboard`, which the coverage test enforces; and a worked example goes in
   `docs/insight-walkthroughs.md` showing a loop skill's rollup beside its self cost, including the
   misreading that invites — a containment total read as though it were causal.

## Consequences

**The contract stops being uniform across artefact types, and that is the honest shape.** A skill can
describe itself, a sub-agent may be able to, an MCP server cannot. Pretending otherwise would mean
either inventing a file for MCP servers that nothing reads, or refusing to measure what we cannot ask
permission to measure.

**An overlay is a local assertion, not an observation.** When an operator declares that a third-party
skill is ticket-scoped, that is a claim about someone else's artefact, and it can be wrong or go stale
when the upstream artefact changes. `SkillManifest.overlay_path` already records that a field came
from an overlay; scope must be covered by the same provenance so an analyst can separate what an
artefact said about itself from what we assumed on its behalf. Under
[ADR-005](005-data-integrity.md) that distinction is the point, not a nicety.

**Adoption is measurable and should be reported.** The doctor already counts how many catalogue entries
were attributed by overlay. Scope declarations should be counted the same way, because "how many
artefacts are scoped, and how many of those we guessed at" bounds every rollup ADR-010 produces.

### Known limitations

- **A declared scope is a hypothesis.** The artefact says it works ticket-by-ticket; the data shows
  what ran. Divergence is a finding, not an error, and neither number is corrected by the other.
- **Overlays go stale silently.** An upstream artefact can change its behaviour without changing its
  name, and the local declaration keeps applying. Provenance makes this visible; nothing makes it
  self-correcting.
- **MCP internal spend stays invisible.** The overlay names an MCP server and scopes its calls. If that
  server spends money calling models of its own, only the server can report it, which puts it in the
  same position as any external process under ADR-010's fifth kind.

## Related

The identity contract, the catalogue, and the overlay direction are
[ADR-004](004-skill-identity-and-catalogue.md). What the declaration is used for is
[ADR-010](010-containment-and-scope.md). The rule that an unobserved value is never recorded, and that
missing data is its own category, is [ADR-005](005-data-integrity.md).
