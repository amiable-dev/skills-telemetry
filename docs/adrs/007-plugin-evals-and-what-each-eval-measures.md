---
title: "ADR-007: Two eval systems, and which question each answers"
status: proposed
date: 2026-09-12
tags: [adr, evaluation, testing, plugin]
links: ["001-distribution-and-capture-surface.md", "005-data-integrity.md", "../evaluation-power.md", "../../eval/run_eval.py"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/10"
---

## Context

This repository already has something called "eval": `eval/run_eval.py` runs a task with and without a
skill and grades the output with OPA. Claude Code ships a different thing also called eval:
`claude plugin eval` runs a plugin against prompts, grades the replies, and compares against a
no-plugin baseline. Both report a with/without delta. Anyone meeting them cold will assume one is
redundant.

They answer different questions, and the gap between them is currently uncovered.

**What our test suite actually verifies about skills and agents: nothing behavioural.** Every claim in
`docs/skills.md` — that `stdtel-onboard` refuses to invent a `standard_id`, that the scorecard agent
refuses a comparison below the sample-size floor — is asserted by reading the markdown for a
substring. One test was until recently named `test_the_agent_treats_refusal_as_a_correct_answer` while
doing exactly that. No test loads a model, so no test observes conduct.

There is also a failure mode upstream of all our telemetry that nothing detects: **a skill whose
`description` does not match natural phrasing is never invoked.** It emits no spans, and the scorecard
shows it as unused — which the analyst is instructed to read as a discoverability problem rather than
ineffectiveness. We cannot currently tell those apart before rollout.

## Options considered

- **Treat `claude plugin eval` as a replacement for `eval/run_eval.py`.** Rejected: it grades replies
  and transcripts, not whether generated code satisfies an OPA policy. The primary effectiveness
  metric — first-time policy pass rate — is not expressible as a reply rubric.
- **Treat `eval/run_eval.py` as sufficient and skip plugin evals.** Rejected: it measures outcome
  quality on a task a skill was *given*, and says nothing about whether the skill gets chosen, or
  whether an agent honours the refusal rules it documents.
- **Assert behaviour with more string checks.** Rejected: it is what we do now, and it produced a test
  whose name claimed conduct it never observed. Verifying model behaviour needs a model.
- **Run plugin evals on every PR.** Rejected as the default. A case is six model runs (three with the
  plugin, three without) plus judge calls, billed to the account. Gating every PR on a non-deterministic
  suite with a real bill is how teams learn to bypass the gate.

## Decision

1. **Keep both, and state the division wherever either is documented.**

   | | question | graded by | deterministic |
   |---|---|---|---|
   | `eval/run_eval.py` | did the skill change the **policy outcome** on a real task? | OPA over generated code | yes |
   | `claude plugin eval` | did the plugin change **Claude's behaviour** on a prompt? | regex / `tool_used` / LLM judge | no |

2. **Adopt `claude plugin eval` for the claims we cannot otherwise test**: that each skill is chosen on
   natural phrasing (`tool_used: Skill`), and that the agent and skills refuse when the documentation
   says they will (`llm` rubric).
3. **Prefer the free graders.** `regex`, `tool_used`, `tool_order` and `file_exists` are computed from
   the transcript and cost nothing; reserve `llm` for the refusal behaviours, where a judge is the only
   instrument.
4. **Run it on a schedule and on plugin changes, not on every PR**, with a cost ceiling and pinned
   models so scores stay comparable.
5. **Name tests for what they check.** A test that reads a file asserts documentation, and its name
   must say so.

## Consequences

- The discoverability failure becomes measurable before rollout rather than inferred from an absence
  of spans afterwards. This is the strongest single reason to adopt it.
- Two systems called "eval" now coexist; the table above is the mitigation and belongs in
  `docs/reference.md` and `docs/skills.md`, not only here.
- A recurring model bill proportional to cases × 6. Kept bounded by the grader choice and schedule.
- Scores are non-deterministic. A failing case is evidence to investigate, not a build break — hence
  scheduled rather than gating.

### Known limitations

- **Not yet implemented.** Status is `proposed`; issue #10 tracks it. Nothing in the repo runs
  `claude plugin eval` today, and the behavioural claims stay unverified until it does.
- A judge model can mark a correct answer wrong for formatting reasons; the docs advise suspecting the
  judge when `tool_used` passes but the delta is negative. Rubrics must not encode formatting.
- Three runs per arm is a small sample against a non-deterministic agent. Deltas near zero should not
  be read as evidence of no effect — the same sample-size caution as
  [evaluation-power.md](../evaluation-power.md), one layer up.
- It tests the plugin as shipped. It cannot tell you whether a skill helps a developer on real work;
  that is what the OPA arm and the delivery join are for.

## Related

`docs/skills.md` states the behavioural claims; until this is adopted they are documentation, not
verified conduct. `tests/test_docs_coverage.py` asserts this ADR exists so the boundary stays visible.
