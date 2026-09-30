---
title: "ADR-013: Join work to change requests by branch and commits, not a ticket-key naming convention"
status: proposed
date: 2026-09-30
tags: [adr, joins, delivery, primary-metric, forge, data-integrity]
links: ["002-delivery-data-joins-and-proxies.md", "005-data-integrity.md", "010-containment-and-scope.md", "012-external-contract-v3.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/102"
---

## Context

The primary metric is first-time policy pass rate on a change request, with and without a skill. It
needs each piece of agent activity joined to the change request its work became. [ADR-002](002-delivery-data-joins-and-proxies.md)
decision 1 made that join a **ticket key parsed from the branch name**. The hook reads the working
tree's branch and stamps `std.ticket.id`. The delivery loader parses each PR's head branch with the
same regex. The two meet on equality.

ADR-002's own reasoning for that choice was that "the join key already exists", which inherits a
decision rather than deriving one. It also called ticket-prefixed branches "a hard requirement rather
than a convention", a requirement no user of the tool is ever told. Measured on 2026-09-30:

- **Real work goes unattributed.** In this repository, which follows its own convention, **2 of 9**
  branches created in one week yielded a ticket. Another repository names branches with the issue
  number last (`fix/media-hardening-233`), and every one of its branches is `unattributed`, so none
  of its work can reach the primary metric.
- **Fake tickets are invented.** Both release branches yield `RELEASE-0`, and **5 of the last 12**
  Dependabot PR branches yield `ANALYZE-4` or `INIT-4`. So unrelated PRs are joined to each other
  across versions. The #62 fix stopped matches in the middle of a path segment, but a version number
  that forms a whole segment still matches.

The regex is not needed for the join. Both sides already observe the branch itself, and the step from
branch to ticket is what adds the convention and manufactures the fake tickets.

The failure is bounded. Session cost, turns, sub-agents, compaction, external spend and ADR-012's
tool-call join need no convention. Two things do depend on it: the outcome join, which carries the
primary metric and ADR-002's crossover arm assignment, and ADR-010's `ticket` scope, which keys every
loop iteration on `std.ticket.id`.

The join also has to stay independent of the forge. Delivery data is GitHub-only today (ADR-002:
"Only GitHub is wired"). That is unavoidable in one sense, since change requests, reviews and CI exist
only in a forge. What can be avoided is letting one forge's shape reach the join or the schema.

## Options considered

- **Keep the regex and fix its false matches.** Rejected as the join key. It still requires a naming
  convention, and it would still miss the trailing-number repository entirely. The fixed regex is kept
  only as a fallback ticket source (decision 5).
- **A per-repository configurable pattern.** Rejected. The hook and the loader would have to agree on
  the pattern for every repository, which is exactly the drift the existing agreement test exists to
  prevent. And a trailing number is the version-number trap: `init-4.38.2`.
- **Join on commit SHAs alone.** Rejected, on evidence. The seven commits this session made on the #87
  branch were rebased and force-pushed, and **none of the seven SHAs** is in PR #87's commit list now.
  Commits also cannot carry the join alone. Most agent cost produces no commit — research, review
  loops, sub-agent exploration, abandoned attempts — and a commit says where code landed, not which
  activity caused it.
- **Join on commit patch-ids alone.** Rejected as the only mechanism, and kept as evidence. `git
  patch-id` hashes the diff, so it survives a rebase: `9049e2a` before and `6aa924d` after both give
  `e8579a76eaae`. It still attributes nothing to turns that commit nothing.
- **Put the plain branch name on spans.** Rejected. A branch name is free text, and it can carry a
  customer or project name into a shared collector or Langfuse. The readable name reaches the
  warehouse from the forge's own record instead.
- **Build the GitLab adapter now.** Deferred to #103. An adapter never run against real data is how
  this project's earlier loader bugs got in.
- **Branch identity plus a time window, confirmed by commit patch-ids, loaded through a forge-neutral
  change-request record** — chosen.

## Decision

1. **Spans carry a branch identity, never a ticket.** `std.branch.hash` is
   `_digest("stdtel-branch", repo_id + "\n" + branch)`, the same scheme as `std.user.hash`. It is a
   resource attribute, refreshed on every event, as #81 did for the ticket. `repo_id` is the
   normalised remote: host, owner and name, lower-cased, with scheme, user and `.git` removed. So
   `git@github.com:amiable-dev/skills-telemetry.git` and
   `https://github.com/amiable-dev/skills-telemetry` are one repository, and a GitLab remote is a
   different one. `std.repo` becomes `owner/name`: today it is the bare name, which collides across
   organisations. `std.ticket.id` leaves the wire.

2. **Spans carry commit evidence.** At each Stop, the hook lists the non-merge commits that appeared
   on `HEAD` since the previous Stop and were authored by the local git identity. It computes their
   patch-ids and attaches them to that turn's activation as `std.artefact.commit_patch_ids`, a list of
   strings. These are opaque content hashes, so metadata only. The session's last-seen `HEAD` is
   persisted in state. After a history rewrite the rewritten commits are listed again, under the same
   patch-ids.

3. **Delivery data loads into a forge-neutral change-request record, through a per-forge adapter.**
   `change_request` replaces `pull_request`. It holds:
   - `cr_id` (`forge:repo_id!number`) and `forge`;
   - `repo_id`, `number`, `source_branch` and `branch_hash`;
   - `opened_at`, `merged_at` and `closed_at`, and `state` (`merged`, `closed` or `open`);
   - `review_rounds`, `hours_to_first_approval`, `ci_failures` and `assisted_by`.

   Commit patch-ids go in `change_request_commit (cr_id, patch_id)`. The adapter computes the same
   branch hash as the hook, and the patch-ids from the forge's per-commit patch: verified on
   2026-09-30, GitHub's patch for `6aa924d` gives the same patch-id as local git, so no clone is
   needed. ADR-002's decisions 2 to 6 carry over unchanged. Only the GitHub adapter is built. GitLab
   is #103. Closed-unmerged change requests are loaded and marked, so abandoned work shows its cost.
   They are excluded from pass-rate comparisons, which need merged changes.

4. **Attribution happens at read time, in one view, and says how it was made.**
   `activation_change_request` assigns an activation on branch hash `H`, at time `t`, to the change
   request on `H` with the earliest close time at or after `t`. If none has closed yet, it is the one
   still open. This handles a branch name reused after a merge. Each assignment records its `method`:
   - `branch` — joined on branch alone;
   - `branch+commit` — joined on branch, and confirmed because a patch-id recorded on that branch is
     in that change request;
   - `commit` — no branch match, but the session's recorded patch-ids landed in a change request. This
     repairs a branch renamed before the PR opened, and a cherry-pick onto another branch.

   The scorecard requires `branch+commit` for the with-arm by default. A change request whose only
   link to a skill is `branch` is **excluded from both arms**, not recruited into the without-arm.
   That is ADR-002's "unknown, never none" rule applied to a weaker join. Every method stays queryable.
   Joining at read time means arrival order does not matter.

5. **The ticket is enrichment, not the key.** `change_request_ticket (cr_id, ticket_id, source)`.
   Sources in priority order:
   - `forge` — the issues the change request says it closes (GitHub `closingIssuesReferences`,
     verified: it returns #86 for PR #87, whose branch contains no key);
   - `title`, `body`, `branch` — a fixed regex that skips `dependabot/`, `renovate/` and `release-`
     branches, and rejects a key followed by `.`.

   A change request may close several tickets. ADR-002's per-ticket arm and `mixed` rule reads this
   table. The primary metric is computed per change request and needs no ticket.

6. **ADR-010's `ticket` scope unit is renamed `branch`**, keyed on `std.branch.hash`. That is what the
   hook actually observes at runtime, when no change request exists yet. `telemetry.scope: ticket` is
   refused with a message naming `branch`. Nothing declares it today.

7. **Every consumer of `std.ticket.id` moves:**
   - the scope key moves to the branch hash;
   - #81's per-event refresh re-derives the branch hash instead;
   - the statusline shows the local branch name, which never leaves the machine;
   - the Langfuse overlay copies the branch hash;
   - the three loader `ticket_id` columns become `branch_hash`;
   - `scorecard.sql` joins through the view.

8. **Prior data is dropped at cutover.** There is a single user, confirmed. The warehouse tables are
   truncated, and spans from before the cutover carry no branch hash, so they cannot join and are not
   backfilled. Removing `std.ticket.id` is a wire change and goes in the release notes.

9. **The hook and the loader must compute the same `repo_id` and branch hash.** A test holds the two
   implementations equal across ssh, https and GitLab-shaped remotes. That is the same guard the two
   regexes had, applied to a join key that cannot produce a false match.

## Consequences

- No naming convention is required anywhere. The trailing-number repository, the release branches
  and the Dependabot branches all join correctly, or honestly fail to, without anyone renaming
  anything.
- Fake tickets end, because a hash cannot match an unrelated branch.
- Activity with no commits still reaches its change request through the branch. Commit evidence
  raises confidence, and repairs renames and cherry-picks.
- Loop containment keys on the branch, so each epic-loop iteration is its own container whatever the
  naming.
- The forge sits behind an adapter. GitLab becomes one new adapter (#103), not a schema change.
- Removing `std.ticket.id` is a breaking change on the wire, and the warehouse is rebuilt from the
  cutover.
- The loader makes one API call per commit to compute patch-ids. That is fine at this scale, and
  should be cached by SHA if volume grows.

### Known limitations

- **Work committed directly to the default branch has no change request**, so it cannot join. It is
  counted as its own category, not dropped.
- **A branch renamed before its PR opens** splits into two hashes. Commit evidence repairs this only
  for work that was committed before the rename.
- **Commits authored under a different git identity**, such as an agent configured with its own email,
  are not recorded as the session's. `user.email` has to be the identity that commits.
- **A commit whose content is edited during an interactive rebase** gets a new patch-id, and loses its
  evidence. The branch join still holds.
- **Copilot's native spans carry no branch hash.** Copilot outcomes stay unjoinable until its
  `github.copilot.git.*` attributes are verified, which is an existing open item.
- **A change request with no linked issue and no key anywhere gets no ticket.** That is correct, and
  it costs nothing at change-request grain.

## Related

- [ADR-002](002-delivery-data-joins-and-proxies.md): decision 1 is superseded by this ADR. Decisions 2
  to 6 stand, over the change-request record.
- [ADR-005](005-data-integrity.md): a fake ticket is a fabricated observation, and `unattributed` as
  the common case is a category that swallows most of the data.
- [ADR-010](010-containment-and-scope.md): the scope unit renamed in decision 6.
- [ADR-012](012-external-contract-v3.md): its tool-call join is independent of this one and unaffected.
- #102 (tracking), #103 (GitLab adapter, backlog), #62 (the partial fix this completes).
