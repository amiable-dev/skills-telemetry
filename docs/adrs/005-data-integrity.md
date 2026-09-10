---
title: "ADR-005: Data integrity — never invent a value, and fail loudly"
status: accepted
date: 2026-09-10
tags: [adr, data-quality, telemetry, testing]
links: ["003-hook-execution-constraints.md", "002-delivery-data-joins-and-proxies.md", "../insight-walkthroughs.md"]
---

## Context

"Hooks always exit 0 so telemetry never blocks the developer" is a good rule with an expensive
consequence: **silent failure is the default failure mode.** An audit of this codebase found six
defects, four of which were silent by construction — the system kept running, kept emitting
well-formed spans, and was wrong.

The four silent ones are worth naming, because they are the same bug in different costumes:

- an empty `transcript_path` resolved to `"."`, a directory, so `Stop` raised `IsADirectoryError`,
  swallowed it, and emitted nothing — no spans, no signal;
- the configured collector endpoint never reached the exporter, which fell back to `localhost` and
  looked like a working pipeline;
- Copilot events arriving through Claude Code's settings were stamped `claude-code`, producing
  perfectly well-formed spans attributed to the wrong harness;
- `std.skill.trigger` was a constant, because the code inferred "explicit" from a leading `/` that no
  real payload has ever carried.

Three more of the same family surfaced later: `grade()` swallowed every exception into a bare
`except`, so a broken grader reported a clean sweep of passes; the manifest policy passed vacuously
because the input document did not collect `.md` files; and the doc generator reported "regenerated 5
tables" while its regex matched nothing.

The pattern in every case: **a component reported success while doing nothing, and the output was
plausible.**

## Options considered

- **Default `trigger` to `"auto"` when unknown.** Rejected: it is a guess presented as a measurement,
  and it is indistinguishable in the warehouse from a real observation.
- **Let hooks raise on error so failures are visible.** Rejected: it violates the rule that telemetry
  must never block the developer, and a telemetry bug would become an outage.
- **Trust the harness's `captureContent: false` flag** for metadata-only enforcement. Rejected:
  microsoft/vscode#326254 reports spans carrying full prompts and responses despite that flag. A
  privacy guarantee that depends on someone else's bug not existing is not a guarantee.
- **Let a policy pass when the thing it grades is absent.** Rejected: vacuous truth. A solution that
  adds no logging would pass a "logging must have required fields" policy, and the with/without arms
  would measure nothing.
- **Rely on code review to catch silent failures.** Rejected on evidence: these shipped past review
  precisely because the code reads correctly. The failures are only visible in output.

## Decision

1. **Never record a value that was not observed.** `std.skill.trigger` reports the transcript's
   `caller.type` or the literal `"unknown"`. Verified necessary: `caller` is absent from the hook
   payload entirely, so the transcript is the only source.
2. **A component that cannot do its job says so on stderr and returns a failing result.** An
   unevaluable policy fails rather than passes; a missing policy root raises; a generator that
   substitutes nothing reports it and exits non-zero.
3. **Vacuous truth is a bug.** Policies deny when the subject they grade is absent — no logging at
   all fails `required_fields`, and a catalogue with no `SKILL.md` in the graded input is a defect in
   the input, not a pass.
4. **Enforce metadata-only at the collector**, not by trusting a client flag, and refuse content
   attribute names in `scrub()` regardless of who supplied them.
5. **Missing data is its own category.** `unattributed` tickets and `unversioned` skills are retained
   for cost analysis and excluded from outcome analysis, never quietly folded into a control group
   (see [ADR-002](002-delivery-data-joins-and-proxies.md)).
6. **Every silent defect gets a regression test named for the failure it prevents**, because the code
   will keep reading correctly.

## Consequences

- Reports contain more "cannot answer" than a naive implementation would. That is the intended
  outcome; [`../insight-walkthroughs.md`](../insight-walkthroughs.md) documents refusal as a correct
  answer, and the scorecard agent refuses below the sample-size floor.
- `unknown` appears in the data as a real category and must be handled in every query, rather than
  being hidden behind a plausible default.
- Test count grew substantially, and most of the new tests assert on absence — that a value was not
  invented, that content did not leak, that a block is not empty.

### Known limitations

- **Loud failure goes to stderr, which is invisible in most harness UIs.** A developer will not see
  it; only CI and a deliberate log check will. Stderr is therefore a weak backstop and the tests are
  the real guard.
- **This ADR cannot prevent the class, only specific instances.** Every case here was found by
  checking output against reality — real transcripts, a live session, a live stack — not by reading
  code. Nothing in the process guarantees the next one is found the same way.
- Retaining `unknown`/`unattributed` rows keeps cost analysis honest but leaves every outcome query
  needing an explicit exclusion. Forgetting one silently biases the result, which is the same class of
  failure this ADR exists to prevent.

## Related

The defects are listed in [ADR-001](001-distribution-and-capture-surface.md); the sample-size floor
that operationalises "refuse rather than guess" is in [`../evaluation-power.md`](../evaluation-power.md).
