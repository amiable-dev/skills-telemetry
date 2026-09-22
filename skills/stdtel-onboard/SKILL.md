---
name: stdtel-onboard
description: Decorate a skill, sub-agent or MCP server to the standards-telemetry contract so its cost can be attributed — adds the metadata block, picks a standard_id and policy_ids, sets telemetry.scope, and writes an overlay entry for artefacts you do not own. Use when onboarding a skill or agent to telemetry, when stdtel-validate fails, when something reports as unversioned, or when a long-running skill's cost needs rolling up.
license: MIT
metadata:
  version: "1.2.0"
  standard_id: STD-TEL-001
  policy_ids: "telemetry.manifest_valid"
  owner: platform-observability
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: policy
---

# Onboard a skill to standards telemetry

A skill that is not in the catalogue still emits spans — it just arrives as
`std.skill.version=unversioned` with no `standard_id` and no `policy_ids`, so it cannot appear in the
scorecard and its cost cannot be attributed to a standard. This skill closes that gap.

## The contract

Agent Skills permits exactly six top-level front-matter keys: `name`, `description`, `license`,
`compatibility`, `metadata`, `allowed-tools`. Anything else is a client-only extension that hard-errors
on claude.ai upload, so **every contract field lives under `metadata:`**, whose values must be strings
(lists are comma-separated).

```yaml
---
name: <matches the directory name>
description: <what it does AND when to use it — this is what the model matches on>
license: MIT
metadata:
  version: "1.2.0"                  # semver, quoted
  standard_id: STD-XXX-000          # the standard this skill operationalises
  policy_ids: "pkg.rule, pkg.other" # OPA packages that verify it; empty only with a non-policy signal
  owner: <team>
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: policy  # policy | test | manual
---
```

## `telemetry.scope` — skills only, and only those that drive work across turns

**This field applies to skills.** A sub-agent may carry one in its front-matter and nothing will read
it: no container is opened for an agent, so the declaration would change no span. It is dropped when
the catalogue loads rather than kept somewhere it does nothing.

Most skills need nothing here. The default is `turn`, and a turn already has real span parentage, so
its contents are attributed without any declaration.

Set `telemetry.scope: ticket` **only** when the skill drives work over many turns — a loop that walks
an epic ticket by ticket is the case this exists for. Its own activation is seconds of tool call while
the work it causes is the largest line item, and without a declared unit that work is attributed to
nothing but individual turns.

`session` is not a value. A session is resumed and persists, so it is not a unit of work; a skill that
runs for a long time is still `turn`-scoped, and the rollup over its scope key is what shows the total.

**A scope you open, you close.** Add `stdtel-hook scope-close` to the skill's own instructions, at
every path that ends the run — the finish, and each safety gate. It takes no arguments: the session id
comes from the environment the harness gives you. Closing when nothing is open is fine, so you never
need to work out which ending happened.

Skip it and the container stays open for the rest of the session, so work you do afterwards is still
attributed to your skill. It closes on its own only when another scoped skill supersedes it, or at the
next fresh start.

Three things follow from declaring it, and all belong in your head before you do:

- **Everything in the window is attributed to it**, including an unrelated question typed mid-loop.
  There is no observable link from an activation to a later tool call, so the number answers *what was
  incurred under this artefact*, never *what it caused*.
- **A declaration is a claim.** If the artefact does not actually work ticket by ticket, the rollup is
  wrong in a way no query can detect.

## Three kinds of artefact

| artefact | where the block goes | notes |
|---|---|---|
| **skill** | `metadata:` in `SKILL.md` | The spec permits exactly six top-level keys, and any other **hard-errors** on upload. Everything lives under `metadata:`. |
| **sub-agent** | `metadata:` in the agent's `.md` | Version, owner and `standard_id` only — **not `telemetry.scope`**. Works, but by *tolerance*: `metadata` is not a documented agent key and unknown keys are ignored rather than specified. Verified against 2.1.277. If that changes, agents silently go back to undecorated — so keep the overlay as a fallback. |
| **MCP server** | an overlay entry only | There is no file to decorate. `.mcp.json` has no free-form metadata field, tool-level `_meta` is declared by the server rather than by you, and the hook payload does not carry it. Calls appear as `mcp__<server>__<tool>` and are counted on the session span. |

Set `STDTEL_AGENTS_ROOT` for agents, the way `STDTEL_SKILLS_ROOT` works for skills. A relative value
resolves against `CLAUDE_PROJECT_DIR`, so `agents` picks up a project's own directory.

## Artefacts you do not own

Editing somebody else's `SKILL.md` or agent file works exactly once: the next upstream release
overwrites it, a plugin reinstall replaces the directory, and a new machine has none of it — all
silently, because the artefact keeps emitting spans that simply arrive undecorated again.

Write an overlay instead. It lives somewhere you control (`STDTEL_SKILLS_OVERLAY`) and supplies only
what the artefact does not state itself. It never overrides: the day upstream starts declaring its own
version, that value wins and your stub goes quiet rather than pinning a number that is no longer true.

An overlay entry is a `SKILL.md` under `<overlay-root>/<artefact-name>/`, carrying only what you can
honestly state. You have no honest `version` for somebody else's artefact — omit it rather than invent
one. Anything an overlay supplied is marked in the data as assumed rather than declared, so nobody
reads your guess as the artefact's own claim.

## Procedure

1. **Read the skill first.** `standard_id` and `policy_ids` are claims about what the skill enforces —
   they must reflect what the body actually says, not be invented to satisfy the validator.
2. **Find or create the policies.** `policy_ids` names OPA packages under `policies/`. If none verifies
   this skill, say so: a skill whose effect cannot be checked has no `success_signal: policy`, and
   `manual` is the honest value. Do not point at an unrelated policy to make the gate pass — that
   makes the primary metric score the skill against a rule it has nothing to do with.

   The validator enforces the pair, not the field: `policy_ids: ""` is accepted **only** alongside
   `telemetry.success_signal: test` or `manual`. `success_signal` defaults to `policy`, so a manifest
   that names no policies must say which other signal it is claiming.
3. **Rewrite the front-matter** into the shape above, preserving the body verbatim.
4. **Decide the scope.** Leave it unset unless the artefact drives work across turns. See above.
5. **Validate**: `stdtel-validate <skills-root>` — the same gate CI runs. Fix what it reports. For an
   agent, also confirm the harness still loads it: `claude plugin validate .` must still pass, because
   the `metadata:` block there is tolerated rather than specified.
6. **Check conformance** with `data.telemetry.manifest_valid.deny`, which catches contract fields
   accidentally left at the top level.

## Things that quietly break attribution

- **The directory name must match `name`.** The catalogue is keyed on the front-matter name.
- **Plugin-provided skills arrive namespaced** (`my-plugin:my-skill`). Telemetry resolves the segment
  after the last `:`, so the catalogue entry stays the bare name — do not rename it to match the plugin.
- **`version` must be quoted.** Unquoted `1.0` is a YAML float and fails the semver check.
- **A sub-agent's block is tolerated, not specified.** It is ignored by the harness today and
  undocumented, so treat a passing `claude plugin validate` as the check that it still works.
- **An MCP server cannot be decorated in place.** Only an overlay describes one, and there is no
  per-call span for it to attach to yet — calls are counted on the session span under their full
  `mcp__<server>__<tool>` name.
- **An empty `policy_ids` means the skill can never be scored.** `scorecard.sql` derives a skill's
  accountable policies by `unnest(policy_ids)`, so an empty list produces no rows in that join and the
  skill reads `insufficient-data` for ever, at any volume. That is the correct answer for a skill
  nothing verifies — but it is a decision, not a default. If you want a verdict, write the policy.
