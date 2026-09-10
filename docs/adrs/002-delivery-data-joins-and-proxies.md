---
title: "ADR-002: Delivery data — join on the branch ticket key, and label every proxy as one"
status: accepted
date: 2026-09-10
tags: [adr, delivery, evaluation, github]
links: ["001-distribution-and-capture-surface.md", "005-data-integrity.md", "../evaluation-power.md", "../../warehouse/load_delivery.py"]
---

## Context

`skill_invocation` says what a skill cost. On its own that supports no decision: cost without outcome
ranks skills by expense and nothing else. The primary metric — first-time OPA policy pass rate, with
versus without the skill — needs delivery data joined to telemetry, which means `ticket`,
`pull_request`, `policy_result` and `defect` need producers.

Three forces shape the answer:

1. **The join key already exists.** `std.ticket.id` is parsed from the git branch at `SessionStart`.
   Anything that reaches the warehouse must be joinable on that key, which makes ticket-prefixed
   branches a hard requirement rather than a convention.
2. **Only GitHub is wired.** `gh` is authenticated and available. Linear, which holds real ticket
   lifecycle timestamps and story points, is not.
3. **The evaluation design is a crossover** (see [`../evaluation-power.md`](../evaluation-power.md)),
   so each ticket needs an arm assignment, and mis-assignment biases the comparison the project
   exists to make.

## Options considered

- **Wait for Linear before building any of it.** Rejected: GitHub alone supports PR-level outcomes,
  review rounds and defects, which is most of the metric set. Waiting would leave the primary metric
  uncomputable indefinitely for a dependency that improves one column.
- **Derive the harness arm from commit trailers or authorship.** Rejected: co-author trailers are
  inconsistently applied and absent entirely from squash merges, so absence would be indistinguishable
  from a no-AI control — the exact failure mode we are trying to avoid.
- **Treat an unlabelled PR as the no-AI control.** Rejected, and this is the most consequential
  rejection here. Missing data would be silently recruited into the control arm, inflating the control
  population with AI-assisted work and biasing every comparison toward "no difference".
- **Count every review as a round.** Rejected: a single thorough reviewer leaving four comments would
  read as four rounds of churn, making careful review look like a quality problem.
- **Recover `run_seq` from the GitHub checks API after the fact.** Rejected: re-runs, retries and
  cancelled jobs make the true ordering unrecoverable, and "first-time pass" is the entire metric. A
  plausible-but-wrong ordering is worse than no data.
- **Compute cycle time from the first commit.** Rejected: local commit timestamps are rewritable and
  frequently predate the work by days after a rebase.

## Decision

1. **`ticket_id` is parsed from the PR branch**, reusing `stdtel.enrich.ticket_from_branch` so the
   telemetry and delivery sides cannot drift apart. A branch with no ticket key yields
   `unattributed`: the PR row is kept (it is real cost) and no `ticket` row is produced (it supports
   no outcome claim).
2. **An unlabelled PR is `unknown`, never `none`.** Only an explicit `no-ai` label creates a control.
3. **`review_rounds` counts `CHANGES_REQUESTED` reviews**, not review comments.
4. **A ticket whose PRs disagree on arm is `mixed`**, for exclusion from crossover comparisons rather
   than a guess at the majority.
5. **`policy_result` comes from a CI artefact**, not the API: CI writes JSONL carrying `run_seq` at
   the moment it knows it. The loader announces loudly on stderr when that artefact is absent, rather
   than reporting an empty table as success.
6. **Cycle time is first-PR-opened to last-PR-merged, and is labelled a proxy** everywhere it
   surfaces. It understates real cycle time, which starts when work starts.

## Consequences

- The metric set is computable today for review rounds, defects, cycle-time proxy and arm assignment;
  first-time pass rate additionally requires CI cooperation, which is a deliberate, visible dependency
  rather than a silent gap.
- **Label discipline becomes load-bearing.** An unlabelled fleet produces `unknown` arms and an empty
  crossover. This is preferable to a silently biased one, but it must be operationally enforced.
- Cycle time will systematically understate. Any comparison using it is comparing proxies, which is
  valid within the dataset and invalid against external benchmarks.
- Re-running the loader is idempotent (`ON CONFLICT DO NOTHING`), so it can run on a schedule.

### Known limitations

- **Tickets split across repositories are invisible as one unit** — the loader is per-repo.
- **`mixed` tickets are dropped from comparisons**, so a team that habitually switches harness
  mid-ticket contributes cost data and no outcome data.
- **Defect linkage is text matching** on the ticket key in issue title or body; a defect that never
  names its ticket is recorded unlinked.
- The proxy cycle time cannot distinguish a ticket that sat unstarted from one that was worked slowly.

## Related

`warehouse/load_delivery.py` implements this; `tests/test_load_delivery.py` pins each judgement call
with a test named for the failure it prevents. The refusal to invent a value is
[ADR-005](005-data-integrity.md).
