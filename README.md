# skills-telemetry

[![PyPI](https://img.shields.io/pypi/v/stdtel)](https://pypi.org/project/stdtel/)
[![Python](https://img.shields.io/pypi/pyversions/stdtel)](https://pypi.org/project/stdtel/)
[![CI](https://github.com/amiable-dev/skills-telemetry/actions/workflows/ci.yml/badge.svg)](https://github.com/amiable-dev/skills-telemetry/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Telemetry and metadata capture for **standards-as-skills** — attributes token cost and outcomes to
individual skills across Claude Code and GitHub Copilot, joined to policy (OPA/Rego), delivery
(Linear/GitHub) and quality data. Design rationale: [docs/design-proposal.md](docs/design-proposal.md).

Metadata only. No prompt, response or file content is ever emitted; the collector drops it again as
defence in depth. If it runs on your machine, [docs/for-developers.md](docs/for-developers.md) lists
every field that leaves it — and `export STDTEL_DISABLED=1` turns it off entirely.

## When can I trust these numbers?

Telemetry starts producing plausible-looking ratios on day one. Most of them mean nothing yet. The
phases below are set by **data volume, not elapsed time** — how long each takes depends entirely on
team size, and a small team may never leave the first one.

| phase | you have | what it supports | what it does not |
|---|---|---|---|
| **Descriptive** | anything below the floor | cost, usage, and finding data-quality faults — `unversioned` skills, `unattributed` branches | any comparison between skills, harnesses, or arms |
| **Directional** | 30+ merged PRs per arm, 5+ developers | spotting large effects (>40%) as a hypothesis, always with an interval | point estimates, or a keep/deprecate decision |
| **Inferential** | 150-400+ PRs per arm, depending on effect size | keep / refine / merge / deprecate decisions | detecting effects under 20%, which needs 900+ |

**The hard floor: below 30 merged PRs per arm, or fewer than 5 developers, report descriptively and make no comparative claim.**

Early on, the most valuable thing this data does is find its own faults. A high share of `unversioned`
skills or `unattributed` tickets bounds every later conclusion, and both are fixable now — see
[`stdtel-onboard`](skills/stdtel-onboard/SKILL.md) and ticket-prefixed branches.

The failure mode this exists to prevent: reading a scorecard after two weeks, seeing a skill
"underperform" across nine PRs, and deprecating it. Nine PRs cannot distinguish a bad skill from a
quiet fortnight. Full derivations and the assumptions behind every figure:
[docs/evaluation-power.md](docs/evaluation-power.md). Worked examples of asking these
questions, including the ones the data cannot answer:
[docs/insight-walkthroughs.md](docs/insight-walkthroughs.md).

## Documentation

| page | for |
|---|---|
| [docs/for-developers.md](docs/for-developers.md) | **if this runs on your machine**: exactly what is collected, and how to switch it off |
| [docs/reference.md](docs/reference.md) | every CLI, its flags and exit codes, and the authoritative `STDTEL_*` table |
| [docs/skills.md](docs/skills.md) | each skill and the agent — when to use, when not to, what it refuses |
| [docs/evaluation-power.md](docs/evaluation-power.md) | how much data before a comparison means anything |
| [docs/insight-walkthroughs.md](docs/insight-walkthroughs.md) | worked examples with real output, including the misreadings |
| [docs/local-stack.md](docs/local-stack.md) | endpoints, credentials, the verification ladder, and how to reset |
| [docs/adrs/](docs/adrs/) | why things are the way they are |

## Layout

```
skills/<name>/SKILL.md      skill catalogue with validated front-matter (the contract)
stdtel/manifest.py          front-matter parser + `stdtel-validate` CI gate
stdtel/hooks/cli.py         Claude Code hooks: session-start | pre-tool-use | post-tool-use | stop
stdtel/transcript.py        incremental JSONL reader + token attribution (tail rule, first-only sensitivity)
stdtel/artefact.py          the capture contract: kinds and their attribute allowlists
stdtel/exporter.py          std.artefact.activation spans via OTLP/HTTP (content scrubbed)
stdtel/enrich.py            join keys: std.ticket.id from branch, std.repo, std.team, std.harness
stdtel/skillmap.py          generates collector/copilot-skill-map.yaml for Copilot tool-call mapping
collector/otel-collector.yaml  drop content → normalise gen_ai.* → map Copilot skills → pseudonymise → spanmetrics
deploy/docker-compose.yml   collector + Tempo + Prometheus + Grafana + Postgres; `langfuse` profile optional
deploy/smoke.sh             eight-hop verification ladder (`mise run smoke`)
collector/overlay-*.yaml    merged over the base config; `none` is the default no-op, `langfuse` adds an exporter
warehouse/schema.sql        skill_invocation, session_cost, ticket, pull_request, policy_result, defect, skill_eval
warehouse/scorecard.sql     weekly per-skill scorecard → keep / refine / review-merge / deprecate
warehouse/load_traces.py    Tempo → Postgres loader
eval/run_eval.py            offline with/without-skill eval, real OPA grading (`--dry-run` is a smoke test)
eval/power.py               generates every table in docs/evaluation-power.md; CI checks it is current
policies/                   Rego behind the primary metric: logging.*, telemetry.manifest_valid
agents/                     skill-scorecard-analyst: keep / refine / merge / deprecate from the data
warehouse/load_delivery.py  GitHub → ticket / pull_request / defect; policy_result from a CI artefact
examples/settings.*.json    hook + OTel wiring: `global` installs once, `project` overrides per repo
docs/adrs/                  six ADRs: distribution, delivery data, hook constraints, identity, integrity, Langfuse
plugin.json                 Agent Plugins v1 manifest (portable `skills/` is the shared half)
.claude-plugin/             Claude Code plugin + marketplace manifest
hooks/, com.github.copilot/ per-harness hook manifests, generated from stdtel/install.py::EVENTS
stdtel/install.py           `stdtel-install`: absolute-path resolution + settings merge
mise.toml                   toolchain (Python 3.13) + `.venv` + tasks wrapping the Makefile
```

## Quick start

Two separate jobs: **install the harness wiring once for your user**, then **onboard each project** you
want attributed telemetry from. Skipping the second step still gives you spans — the skills just come
through as `unversioned`, with `std.team=unknown`.

### 1. Install once, globally

```bash
uv tool install stdtel        # or: pipx install stdtel
stdtel-install settings       # merges hooks into ~/.claude/settings.json
```

> **Two halves, and neither updates the other.** The plugin ships hook registration, the launcher,
> the skills and the agent. The **package** ships everything that runs. A plugin without the package
> produces **no data and no error**, because the launcher exits 0 in silence — it says so once at
> session start, which is the only place it can, since `stdtel-doctor` is part of the package too.
> Keep them in step:
>
> ```bash
> uv tool install stdtel --force --refresh          # the package
> claude plugin update stdtel@amiable-standards     # the plugin
> ```
>
> `stdtel-doctor` reports drift between them. To run from a checkout instead:
> `uv tool install /path/to/skills-telemetry`.

`stdtel-install` resolves the **absolute path** of the `stdtel-hook` it was installed alongside and
writes that into the config. This is not cosmetic: hook processes get a non-login `sh -c` and inherit
whatever PATH launched the harness, so a bare `stdtel-hook` is unresolvable whenever a version manager
(mise, asdf, pyenv) or an activated venv is what put it there. The same reason pre-commit bakes
`sys.executable` into the git hook it generates. `stdtel-install where` prints the path it will use;
`--dry-run` shows the JSON without writing.

Claude Code also **strips `OTEL_*` from every subprocess it spawns**, so point the exporter at your
collector with the `STDTEL_`-namespaced variables, which survive:

```bash
export STDTEL_OTLP_ENDPOINT=http://collector.internal:4318   # default: http://localhost:4318
export STDTEL_OTLP_TIMEOUT=2                                 # seconds; bounds a dead-collector stall
```

With no `STDTEL_SKILLS_ROOT` set, the catalogue is read from `~/.claude/skills`. Symlink your
standards there and every project gets versioned spans:

```bash
ln -s "$PWD/skills/structured-logging" ~/.claude/skills/structured-logging
stdtel-validate ~/.claude/skills      # same contract gate CI runs
```

#### As a plugin

The repo is laid out for three loaders at once (ADR-001), so it installs as a plugin without a
separate packaging step:

```bash
/plugin marketplace add amiable-dev/skills-telemetry
/plugin install stdtel@amiable-standards
```

The shipped `hooks/hooks.json` carries the bare command name, because a distributed manifest cannot
know your install path — run `stdtel-install settings` afterwards to bind it to an absolute one.

#### Copilot

Copilot needs **no code from us**. It emits first-party OpenTelemetry with per-tool-call spans and
token counts; point it at the same collector (user settings, `COPILOT_OTEL_*` env vars, or the
enterprise `managed-settings.json` `telemetry` block for a fleet).

If you do run our hooks on Copilot as well, install them from `com.github.copilot/hooks/hooks.json`,
which sets `STDTEL_HARNESS` per hook. That is load-bearing: VS Code Copilot **reads
`~/.claude/settings.json`**, and its snake_case payload dialect is indistinguishable from Claude
Code's — without that env block, Copilot activity is recorded as `claude-code`.

### 2. Onboard a project

Per-project overrides go in `<project>/.claude/settings.json` — Claude Code merges them over the user
file, so repeat only what differs. Do **not** repeat the `hooks` block: it is already registered globally
and a second copy fires each hook twice.

```bash
cd ~/projects/payments-api
mkdir -p .claude && cp ~/projects/skills-telemetry/examples/settings.project.json .claude/settings.json
$EDITOR .claude/settings.json      # STDTEL_TEAM is the one you must set
git checkout -b feature/PLAT-123-add-audit-log   # ticket prefix -> std.ticket.id join key
```

| variable | where | meaning |
|---|---|---|
| `STDTEL_TEAM` | project | owning team on every span; `unknown` until you set it |
| `STDTEL_HARNESS_MODE` | project | `agent` / `interactive` — keeps the Claude Code vs Copilot split fair |
| `STDTEL_SKILLS_ROOT` | project | extra catalogue root(s), `os.pathsep`-separated. A relative path resolves against the project directory; `~/.claude/skills` is always searched last, and the earliest root wins a name collision |
| `STDTEL_HARNESS`, `OTEL_*` | global | harness label and collector endpoint |

Then verify the loop end to end:

```bash
claude                                     # invoke a skill in the project
cat ~/.stdtel/sessions/*.json              # a window with skill, version, tool_use_id
curl -s 'http://localhost:3200/api/search?tags=name%3Dstd.artefact.activation' | jq '.traces[0]'
```

A `std.skill.version` of `unversioned` means the name in the transcript matched no `SKILL.md` in any root
— check `STDTEL_SKILLS_ROOT` and that the skill's front-matter `name` matches what you invoked.

Copilot: enable managed OTel export (VS Code / CLI) pointing at the same collector with resource attributes
`std.harness=copilot-vscode`, `std.team=<team>`; `collector/otel-collector.yaml` maps catalogued skill
tool-calls onto `std.skill.*`.

## Span schema

Two span types, emitted at `Stop`. **They overlap by design and must never be summed:**
`std.session.cost` is the session's total spend, `std.artefact.activation` attributes a *share* of that
total to one skill. Use session cost as the denominator for cost-per-PR; use invocation tail tokens to
compare skills with each other.

### `std.session.cost`

Emitted once per turn that made any LLM request, **whether or not a skill was loaded** — a session that
never loads a skill is still spend, and excluding it would silently understate cost per PR.

| attribute | source |
|---|---|
| `gen_ai.usage.{input,output,cache_read_input,cache_creation_input}_tokens` | whole transcript slice |
| `std.session.llm_requests`, `gen_ai.request.model` | transcript |
| `std.session.tool_calls`, `std.session.tool_failures` | every tool call in the turn |
| `std.session.tool.<name>.{calls,failures}` | per tool; `failures` omitted when zero |
| `std.ticket.id`, `std.repo`, `std.team`, `std.harness` | resource (SessionStart) |

### `std.artefact.activation`

One span name for four artefacts, discriminated by `std.artefact.kind`. The per-kind attribute
allowlist in `stdtel/artefact.py` is the contract — an attribute belonging to another kind is dropped
and reported rather than emitted (ADR-009).

| kind | `std.artefact.name` | what else it carries |
|---|---|---|
| `skill` | catalogue name | the contract fields below |
| `subagent` | agent type (`Explore`, `general-purpose`) | `std.subagent.{id,type,depth,llm_requests,tool_calls,duration_ms}`, tokens summed from the sub-agent's own transcript |
| `compaction` | reason (`auto`, `manual`) | `std.compaction.{reason,tokens_before,tokens_after,turns_since_previous}`, the harness's own estimates |
| `turn` | **none** — a turn is identified by `std.prompt.id`, which is unbounded and must never become a metrics dimension | `std.turn.{llm_requests,tool_calls,duration_ms,hook_ms}` and `std.turn.hook.<basename>.ms` |

Every kind also carries `std.artefact.source`: `hook` when the harness reported it, `transcript` when
it was read from the session directory because the hook has never fired on this machine. An inference
and an observation are not the same measurement and a query can tell them apart.

`kind=skill` additionally carries:

| attribute | source |
|---|---|
| `std.skill.name/version/trigger`, `std.standard_id`, `std.policy.ids` | hook + manifest |
| `std.skill.invoked_as`, `std.skill.plugin` | raw invocation string (plugin skills are namespaced) |
| `std.skill.content_hash` | SHA-256 of the SKILL.md **body**, truncated. The version is asserted; this is observed — one version with two hashes is an edit that skipped the bump |
| `std.skill.load_tokens`, `std.skill.tail_tokens`, `std.skill.tail_tokens_first_only`, `std.skill.llm_requests` | transcript attribution |
| `gen_ai.usage.{input,output,cache_read_input,cache_creation_input}_tokens`, `gen_ai.request.model` | transcript |
| `std.ticket.id`, `std.repo`, `std.team`, `std.harness`, `std.harness.mode` | resource (SessionStart) |
| `std.user.hash` | hook, already SHA-256 of uid + hostname (the collector also pseudonymises `user.email` if a harness supplies one) |

## Known limitations

- Tool counts are counts only. `PostToolUse` fires for **every** tool, so `tool_input` and
  `tool_response` carry commands, file contents and diffs — `scrub()` refuses those attribute names
  outright and a test asserts nothing leaks.
- Skill name is parsed from the Skill tool input in hooks (the field is `skill`, verified against 120 real invocations). Parsing is isolated in `hooks/cli.py::_skill_from_payload`.
- `std.skill.trigger` reports the transcript's `caller.type` where present, else `unknown` — it is never guessed.
- Tail attribution splits by load order; when several skills load in one turn compare against `tail_tokens_first_only`.
- Copilot granularity is per turn; use Claude Code's finer data for within-harness tuning only.
- `load_tokens` uses a chars/4 heuristic on the Skill tool result.
- A turn emits one span per `Stop` carrying that slice's **delta**, so two Stops inside one turn
  produce two rows that sum correctly. Count turns with `count(DISTINCT prompt_id)`, never `count(*)`.
- `subagent` and `compaction` are Claude Code only. Copilot has no confirmed equivalent, so those
  hooks are not registered there rather than registered and silently never firing.
- `gen_ai.*` conventions are still *Development* upstream; extend `transform/normalise` as names move.

## Contributing

Bug reports, questions and PRs are all welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) — it covers the setup (`mise run ci` is the whole gate) and the
conventions that the tests enforce, including the ones that will surprise you.

| | |
|---|---|
| something is broken, or you have an idea | [issues](https://github.com/amiable-dev/skills-telemetry/issues) |
| a question, or "is this supposed to happen" | [discussions](https://github.com/amiable-dev/skills-telemetry/discussions) |
| getting it working at all | [SUPPORT.md](SUPPORT.md) |
| a security vulnerability | **privately**, see [SECURITY.md](SECURITY.md) |
| how to behave here | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

This tool runs inside your coding sessions, so [SECURITY.md](SECURITY.md) is also where the honest
answer to "what does it collect" lives — as a list, not an assurance.

## License

[MIT](LICENSE).
