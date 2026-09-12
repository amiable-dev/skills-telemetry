---
title: "ADR-001: Distribution — vendor-specific hooks in a dual-format plugin, installed by uv tool"
status: accepted
date: 2026-09-10
tags: [adr, distribution, telemetry, cross-harness]
links: ["../design-proposal.md", "../../README.md", "../../CLAUDE.md"]
research: "2026-09-10, four parallel research threads (Claude Code channels, Copilot surface, Python CLI bootstrap, cross-agent standards); claims marked [V] were re-verified locally against docs, code or transcripts before acceptance"
---

## Context

`stdtel` must reach developer machines to be worth anything, and the question of *how* was
unresolved. Four forces, in the order they constrain the answer:

1. **Both harnesses are first-class.** The project's headline claim is a fair Claude Code vs Copilot
   comparison. A channel that only reaches one harness biases the very measurement the project exists
   to make, so "ship a Claude Code plugin and do Copilot later" is not available.
2. **Hooks execute on every tool call.** Measured `[V]`: the bare interpreter floor on this machine is
   10ms; our hook was 40ms when `hooks/cli.py` imported OpenTelemetry and PyYAML at module scope, and
   20ms after those imports were made function-local. At roughly two hooks per call and ~30 calls per
   prompt, that difference is ~1s per interaction. Distribution choices that add per-invocation
   overhead (`uvx`: +24ms; `npx`: 272ms; PyInstaller `--onefile`: 302ms) are therefore disqualifying,
   not merely suboptimal.
3. **Hook processes get a scrubbed, non-login shell.** Claude Code runs hook commands under `sh -c`
   and **removes `OTEL_*` from every subprocess it spawns** `[V, quoted from code.claude.com/docs/en/hooks]`.
   PATH is inherited from whatever launched Claude Code, so anything relying on shell-rc activation
   — mise, asdf, pyenv, an activated venv — is not on PATH when a hook fires.
4. **OSS-first, enterprise-pinnable.** The install must be one command for a stranger and hard-pinnable
   for an org, without us running any infrastructure.

Two premises we started from turned out to be false, and both change the answer:

- **Copilot is no longer the weaker surface.** It now has a hook system of 13–14 lifecycle events with
  the same stdin-JSON/exit-code contract, *and* first-party OpenTelemetry export emitting
  `invoke_agent`/`chat`/`execute_tool` spans with input, output and cache token counts. Its
  enterprise channel — one `managed-settings.json` in a `.github-private` repo, propagating fleet-wide
  in ~an hour with no MDM — is lower-friction than anything on the Claude Code side.
- **Claude Code ships per-skill token attribution natively**, on `claude_code.token.usage` with a
  `skill.name` attribute. Capture is no longer the differentiator; the OPA join and the with/without
  design are.

And one thing we assumed existed does not: **no vendor-neutral standard covers hooks.** Agent Plugins
1.0 (published 2026-07-24, announced 2026-08-06, GA across Copilot surfaces 2026-08-12) fixes exactly
two component locations — `skills/` and `mcp.json` — and states that commands, hooks, agents, rules and
LSP servers "remain too client-specific for a stable portable contract". Its reverse-domain directory
mechanism is explicitly a non-contract: the spec "assigns no portable discovery, validation, loading,
or failure semantics" to it. A serious portable-hooks proposal (Discussion #54, backed by 12 shipping
integrations) has had zero maintainer engagement in four weeks, and the spec repo has landed one commit
since 2026-08-19. Anthropic is not on the TSC and Claude Code is non-conformant by construction — it
reads `.claude-plugin/plugin.json`, not the root `plugin.json` the spec requires.

## Options considered

- **A single Claude Code plugin.** Rejected on force 1: it cannot reach Copilot, and would bias the
  comparison toward the harness we instrument better.
- **Wait for a portable hooks standard, ship nothing.** Rejected: realistic timeline 12–24 months and
  quite possibly never, on evidence of maintainer silence and a contribution policy that requires
  implementor sponsorship no TSC member has offered.
- **`uvx`-per-invocation.** Rejected on force 2: +24ms on every call, and it fails outright against a
  cold cache — which is exactly what first-run-after-install looks like.
- **Standalone binary via PyInstaller.** Rejected on measurement: `--onefile` is 302ms, twenty times
  slower than shipping no binary at all, because it unpacks itself per run. Also needs Developer ID
  signing and notarisation on macOS, and a per-OS CI matrix.
- **npm-distributed binary.** Rejected: npm 12 (2026-07-08) and pnpm 10 disable install scripts by
  default, the Node shim costs ~21ms, and our payload has no platform variance to justify it.
- **Rewrite the hot path in Rust/Go now.** Deferred, not rejected: worth ~13ms/call against the cost of
  maintaining two implementations. Revisit if hooks ever fire on more than tool calls, or if a
  Python-free MDM rollout becomes a requirement.
- **Reimplement Copilot capture with our own hooks.** Rejected: it reimplements shipped functionality,
  adds a Python runtime requirement to every Copilot machine, and Copilot hooks fire on *tool* use
  while Copilot skills are injected into context — so they cannot see skill identity anyway.

## Decision

1. **Ship one repository laid out to satisfy three loaders at once.** Root `plugin.json` for Agent
   Plugins v1 conformance; one portable `skills/` tree; hook manifests in the two vendor namespaces.
   Only `skills/` and `mcp.json` are reserved by the spec, so every other top-level path is free and
   this is additive rather than a fork.

   ```
   plugin.json                            # Agent Plugins v1 conformance
   skills/                                # portable; every loader reads this
   com.github.copilot/hooks/hooks.json    # Copilot + VS Code
   .claude-plugin/plugin.json             # Claude Code
   hooks/hooks.json
   ```

2. **The hooks stay vendor-specific, deliberately.** We adopt the six-event vocabulary
   (`session-start`, `turn-start`, `pre-tool-use`, `post-tool-use`, `turn-end`, `session-end`) as our
   internal normalised form — `hooks/cli.py` already dispatches on almost exactly this — so that if a
   portable component ever lands, migration is config generation, not a rewrite.

3. **`uv tool install stdtel` from PyPI is the install channel**, invoked by **absolute path in exec
   form** (`command` + `args`), never by bare name and never through a shell. `uv tool` shims exec the
   tool venv's interpreter directly (+1–2ms) and bind to a uv-managed CPython rather than whatever a
   version manager has on PATH — which is precisely force 3's problem. `pipx install stdtel` is the
   documented alternative.

4. **Copilot is instrumented by configuration, not by our code.** Native OTel export via enterprise
   managed settings; no Python, no binary, no hooks on that side. Our collector normalises. This
   reverses the assumption behind `collector/copilot-skill-map.yaml`, which stays only as a fallback
   for clients that do not emit `skill_name` — pending verification (see Consequences).

5. **Never import the OpenTelemetry SDK on a per-tool-call path.** This is a load-bearing invariant
   with a regression test (`test_hook_module_import_stays_stdlib_only`), not an optimisation. Prior
   art: semgrep pays 117ms per invocation to this exact mistake.

6. **The exporter fails fast and reads a `STDTEL_`-namespaced endpoint.** `OTEL_*` cannot reach a hook
   (force 3), and `force_flush(timeout_millis=…)` is ignored upstream
   ([opentelemetry-python#4043]), so the timeout must be set on the exporter itself.

## Consequences

- **A future compiled rewrite costs nothing at the distribution layer.** Ruff ships a Rust binary
  through PyPI via maturin `bindings = "bin"` — the wheel carries no console script and the binary
  sits in `.data/scripts/`, copied verbatim into `bin/`. Swapping our implementation keeps the install
  command, the pinning story and the settings path identical.
- **We are not betting on Agent Plugins for the part that matters.** We take its `skills/` contract,
  which is real and shipping in nine clients, and keep hooks in vendor namespaces, which is where the
  spec itself puts them. If Discussion #54 ever lands we adopt it; if it never does, nothing breaks.
- **Both spec dependencies are effectively one-maintainer projects.** 78 of 81 Agent Plugins commits
  are by a single person, who is also the dominant committer on Agent Skills — which has no version
  number, no releases and no deprecation policy at all. Governance charters and bus factor are
  different things; pin by commit SHA, do not assume a release cadence.
- **Cost columns are not comparable across harnesses.** Copilot reports AI Credits/AIU, Claude Code
  reports USD. Any fairness claim must compare tokens, or state its conversion assumption explicitly.
- **A structural asymmetry must be disclosed, not papered over.** Copilot hooks fire on tool use and
  Copilot skills are injected into context, so Copilot cannot yield skill attribution the way Claude
  Code can. This is a caveat on the comparison, not a bug to fix.
- **Unverified, and load-bearing if we act on it:** the `skill_name` and `github.copilot.git.*`
  attributes come from a doc source contradicted by one mirror — check them against a live trace
  before deleting the skill map. Copilot's PascalCase compatibility mode is documented but unexecuted.
  And microsoft/vscode#326254 reports spans carrying full prompts and responses *even with
  `captureContent: false`*, which collides with our metadata-only rule: enforce the filter
  collector-side rather than trusting the flag.

## Defects this research surfaced, now fixed

Auditing the code against the above found six, four of which were silent by construction because hooks
exit 0 on purpose. All are fixed with regression tests (`tests/test_regressions.py`):

| | Defect | Evidence |
|---|---|---|
| 1 | `Path("")` is `"."`, a directory — `stop` died with `IsADirectoryError` and lost all telemetry | reproduced `[V]` |
| 2 | Configured endpoint never reached the exporter; it silently defaulted to localhost | `OTEL_*` scrub `[V]` |
| 3 | Dead collector stalled the Stop hook 7.34s per turn | measured `[V]`, now 0.91s |
| 4 | Copilot events arriving via `.claude/settings.json` were stamped `std.harness=claude-code` | reproduced `[V]` |
| 5 | Namespaced skill names never matched the catalogue → `unversioned` | **85 of 120 real invocations** `[V]` |
| 6 | `std.skill.trigger` was a constant — no real payload has a slash or an `explicit` key | 120 invocations `[V]` |

Defect 5 is the one that matters most here: it is caused *by* plugin distribution. Shipping the plugin
without it would have silently voided version, `standard_id` and `policy_ids` on ~71% of spans.

## Related

- Known gap #3 in CLAUDE.md is **closed**: the Skill tool's input field is `skill`, verified across
  120 invocations in 114 transcripts under `~/.claude/projects/`. One research thread asserted
  `skill_name`; that is the OTel event surface, a different payload. An `args` field also appears and
  is deliberately not captured — it can carry content.
- **Time-sensitive:** OpenTelemetry filed `gen_ai.skill.*` conventions on 2026-09-04
  ([semantic-conventions-genai#501], [PR #498]) — six days before this ADR, still shapeable. Its
  motivation reads like our design proposal, and it names the Claude Agent SDK as the case its author
  could not instrument, because tools run inside the CLI subprocess. That is exactly what we built.
  Commenting there is the highest-leverage action available.
- The `gen_ai.*` conventions moved out of the main semconv repo to
  `open-telemetry/semantic-conventions-genai` (2026-05-05); CLAUDE.md's note should be retargeted.
- Superseded assumption: the Copilot Metrics API we planned around was **shut down 2026-04-02**. Its
  replacement returns signed NDJSON download URLs, carries no skill identity, and lags ~3 days — so it
  is a population denominator, not an attribution feed.

[opentelemetry-python#4043]: https://github.com/open-telemetry/opentelemetry-python/issues/4043
[semantic-conventions-genai#501]: https://github.com/open-telemetry/semantic-conventions-genai/issues/501
[PR #498]: https://github.com/open-telemetry/semantic-conventions-genai/pull/498
