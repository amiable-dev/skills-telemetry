---
name: stdtel-onboard
description: Bring an existing SKILL.md up to the standards-telemetry contract so the skill can be measured — adds the metadata block, picks a standard_id and policy_ids, and validates. Use when onboarding a skill to telemetry, when stdtel-validate fails, or when a skill reports as unversioned in the data.
license: MIT
metadata:
  version: "1.0.0"
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
  policy_ids: "pkg.rule, pkg.other" # OPA packages that verify it — never empty
  owner: <team>
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: policy  # policy | test | manual
---
```

## Procedure

1. **Read the skill first.** `standard_id` and `policy_ids` are claims about what the skill enforces —
   they must reflect what the body actually says, not be invented to satisfy the validator.
2. **Find or create the policies.** `policy_ids` names OPA packages under `policies/`. If none verifies
   this skill, say so: a skill whose effect cannot be checked has no `success_signal: policy`, and
   `manual` is the honest value. Do not point at an unrelated policy to make the gate pass.
3. **Rewrite the front-matter** into the shape above, preserving the body verbatim.
4. **Validate**: `stdtel-validate <skills-root>` — the same gate CI runs. Fix what it reports.
5. **Check conformance** with `data.telemetry.manifest_valid.deny`, which catches contract fields
   accidentally left at the top level.

## Things that quietly break attribution

- **The directory name must match `name`.** The catalogue is keyed on the front-matter name.
- **Plugin-provided skills arrive namespaced** (`my-plugin:my-skill`). Telemetry resolves the segment
  after the last `:`, so the catalogue entry stays the bare name — do not rename it to match the plugin.
- **`version` must be quoted.** Unquoted `1.0` is a YAML float and fails the semver check.
- **`policy_ids` must be non-empty.** An empty list means the skill can never be scored.
