---
title: "ADR-004: Skill identity — spec-conformant front-matter, bare-name catalogue keys"
status: accepted
date: 2026-09-10
tags: [adr, skills, catalogue, agent-skills]
links: ["001-distribution-and-capture-surface.md", "../../stdtel/manifest.py", "../../stdtel/hooks/cli.py", "../../skills/stdtel-onboard/SKILL.md"]
evidence: "120 real Skill invocations across 114 transcripts in ~/.claude/projects (2026-09-10)"
---

## Context

A skill's identity is the primary key of this entire system: it keys the catalogue, the span
attributes, the warehouse and the scorecard. Three facts constrain how it can work.

1. **Agent Skills permits only six top-level front-matter keys** — `name`, `description`, `license`,
   `compatibility`, `metadata`, `allowed-tools`. Claude Code's docs state that other fields are
   Claude-Code-only extensions and that including unsupported fields in a claude.ai upload is a hard
   error. Our contract needs six more fields than the spec allows.
2. **Skill names arrive namespaced.** Measured across 120 real invocations: **85 of 120 (71%)** are
   of the form `plugin:skill` (`epic-loop:epic-loop`, `anthropic-skills:skill-creator`). Every skill
   looks like this once distributed as a plugin — which is the distribution decision in
   [ADR-001](001-distribution-and-capture-surface.md).
3. **The catalogue is read by two callers with opposite needs**: CI wants a strict contract gate that
   fails on any invalid manifest, and hooks want never to break the developer.

## Options considered

- **Keep contract fields at the top level.** Rejected: non-conformant, and hard-errors on claude.ai
  upload. It also makes the portable `skills/` tree in ADR-001 a fiction.
- **Key the catalogue on the full namespaced name.** Rejected: the same skill would need a separate
  entry per plugin that ships it, and the catalogue would have to be rewritten whenever distribution
  changed. Identity would become a property of packaging rather than of the skill.
- **Rename catalogue entries to match the plugin.** Rejected for the same reason, and it would break
  `stdtel-validate`, which requires `name` to match the directory.
- **Strip the namespace and record only the bare name.** Rejected: the namespace is real information —
  which plugin supplied the skill — and is exactly what a plugin-distribution rollout wants to group
  by.
- **Load the catalogue strictly in hooks.** Rejected: one malformed `SKILL.md` in a shared
  `~/.claude/skills` would raise and blank the entire catalogue, marking every skill `unversioned`.
  One person's broken file would silence everyone's telemetry.
- **Require `STDTEL_SKILLS_ROOT` to be absolute.** Rejected: it forces per-machine configuration into
  a checked-in project settings file.

## Decision

1. **Contract fields live under `metadata:`**, whose values the spec types as strings, so lists are
   comma-separated. The original flat form still parses, so existing catalogues keep validating;
   `metadata:` wins when both are present.
2. **The catalogue is keyed on the bare front-matter `name`.** Resolution tries the full invoked
   string first, then the segment after the last `:`.
3. **Both names are recorded**: `std.skill.name` is the catalogue name (stable, low cardinality),
   `std.skill.invoked_as` is the raw string, and `std.skill.plugin` is the namespace when present.
4. **Root precedence**: `STDTEL_SKILLS_ROOT` takes an `os.pathsep` list, relative entries resolve
   against `CLAUDE_PROJECT_DIR`, `~/.claude/skills` is always searched last, and the earliest root
   wins a collision.
5. **Hooks load leniently, CI loads strictly.** `load_catalogue(..., strict=False)` skips unparseable
   manifests and duplicate names; `stdtel-validate` raises on both.
6. **The scanner follows symlinked directories.** `Path.rglob` does not, which silently broke the
   documented `ln -s` into `~/.claude/skills` workflow.

## Amendment (2026-09-14): `policy_ids` may be empty, `success_signal: policy` may not

The contract originally required a non-empty `policy_ids` on every skill. In practice that is a rule
nobody can satisfy honestly: plenty of useful skills — an indexing tool, a viewer, a research aid —
have no deterministic check, and the gate pushed people to cite an unrelated policy to get past it.
That is worse than an empty list, because `scorecard.sql` would then score the skill against a rule it
has nothing to do with, and the number would look like evidence.

The rule now binds the **pair**: `policy_ids` may be empty only when `telemetry.success_signal` is
`test` or `manual`. The signal defaults to `policy`, so a manifest naming no policies must say what it
is claiming instead. An empty list still means the skill reads `insufficient-data` for ever — which is
the correct answer for something nothing verifies, and is now a decision the manifest states rather
than a gap it hides.

## Amendment (2026-09-14): overlays fill gaps; they never override

Onboarding a skill by editing its `SKILL.md` assumes we own the file. For third-party and
plugin-provided skills we do not, and the edit survives exactly until the next upstream release,
plugin reinstall, or new machine — silently, because the skill keeps emitting spans and they simply
arrive `unversioned` again.

`STDTEL_SKILLS_OVERLAY` names roots of front-matter-only stubs that supply what a skill does not state
about itself. The precedence is deliberately the reverse of `STDTEL_SKILLS_ROOT`, where the earliest
root wins outright: an overlay that overrode would keep asserting its pinned version after upstream
began declaring a real one, and nothing would notice. Filling gaps means upstream wins the moment it
has something to say.

Two consequences worth stating. A stub may omit `version`, because the operator has no honest one for
somebody else's artifact — `std.skill.content_hash` identifies it instead, and cannot go stale. And a
stub never supplies a content hash: its body is the operator's note, not the skill's instruction, so
hashing it would put a meaningful-looking value where nothing was observed.

Lenient loading changed with it. A manifest that fails the contract is no longer dropped; it is kept
in degraded form, carrying its name and content hash. Dropping it cost us the one thing about an
un-onboarded skill that can be observed rather than asserted.

## Consequences

- The shipped catalogue is spec-conformant and uploadable, while remaining fully validated by our own
  stricter gate. `telemetry.manifest_valid` makes conformance measurable rather than advisory.
- Plugin distribution no longer voids attribution. Before bare-suffix resolution, ~71% of real
  invocations would have carried `unversioned` with no `standard_id` and no `policy_ids`.
- Dashboards group on `std.skill.name` and stay stable across repackaging; `std.skill.plugin` gives
  the rollout view for free.

### Known limitations

- **Two skills with the same bare name in different plugins collide.** `a:logging` and `b:logging`
  both resolve to catalogue entry `logging`. The first root wins, silently. This is the direct cost of
  choosing packaging-independent identity, and it is the right trade only while bare names stay
  unique within an organisation's catalogue — which nothing enforces.
- **Lenient loading hides broken manifests from the developer.** They surface only in CI, so a skill
  can be quietly `unversioned` locally for a long time. `stdtel-onboard` exists partly to shorten that
  loop — and until #49, the strict gate reported one failure at a time without naming the file, so
  the loop it shortened was still a bisect.
- `metadata:` values are strings, so `policy_ids` round-trips through comma splitting; a policy id
  containing a comma would break, and nothing validates against that.

## Related

`skills/stdtel-onboard` is the operational counterpart — it decorates an existing `SKILL.md` to this
contract. Gap #3 in `CLAUDE.md` was closed by the same evidence: the Skill tool's input field is
`skill` in all 120 observed invocations (`skill_name` is the OTel event surface, a different payload).
