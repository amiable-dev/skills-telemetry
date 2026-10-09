# CLI reference

The console scripts below. `stdtel-hook` is invoked by the harness one process per event; its only
subcommand meant for a human or a skill is `scope-close`. The other three scripts are yours.

All of them read the [environment variables](#environment-variables) below.

---

## `stdtel-install`

Renders hook configuration bound to **this** installation's absolute path, and merges it into a
settings file. Run it after installing the package or the plugin — a distributed hook manifest carries
the bare command name and is deliberately one step short of working, because it cannot know your path.

```
stdtel-install where
stdtel-install hooks    [--harness claude-code|copilot-vscode|copilot-cli] [--exec-form]
stdtel-install settings [--path PATH] [--exec-form] [--dry-run] [--no-native | --native-only] [--replace-endpoint]
stdtel-install copilot  [--vscode-settings PATH]
stdtel-install detailed-view on|off [--path PATH] [--collector-confirmed]
```

| subcommand | does |
|---|---|
| `where` | prints the absolute `stdtel-hook` path it will write. Start here when hooks silently do nothing |
| `hooks` | prints the hook JSON for a harness to stdout; pipe it wherever you keep config |
| `settings` | merges the Claude Code hook block **and Claude Code's own telemetry** (`env`, ADR-014 decision 2) into a settings file, preserving everything already there, including a detailed view you have already switched on. It never writes `OTEL_LOG_TOOL_DETAILS` or `OTEL_METRICS_EXPORTER`, and reports but leaves alone an `OTEL_METRICS_EXPORTER` it finds |
| `detailed-view` | `on` writes `OTEL_LOG_TOOL_DETAILS=1` into Claude Code's settings, **only** if the collector is on this machine (or `--collector-confirmed` says you know it applies these rules) **and** the content probe (`stdtel-doctor --content-check`) passes right then; otherwise it exits `1` and writes nothing. `off` removes the flag alone. The `stdtel-setup` skill runs `on` only after asking you. ADR-014 decisions 12 and 13 |
| `copilot` | prints Copilot's native OTel settings for VS Code (`github.copilot.chat.otel.*`, `captureContent: false`) and the Copilot CLI (`COPILOT_OTEL_*`). With `--vscode-settings PATH` it merges into that file instead, and refuses a file with comments rather than rewriting them away. Needs no `stdtel-hook`. For an organisation, the enterprise-managed `telemetry` policy is the better route |

| flag | default | meaning |
|---|---|---|
| `--harness` | `claude-code` | which dialect to emit. The Copilot forms set `STDTEL_HARNESS` per hook, which is **required** — see [ADR-003](adrs/003-hook-execution-constraints.md) |
| `--exec-form` | off | emit `command` + `args` instead of a shell string. Saves ~3ms per call by skipping `sh -c`; verified working against Claude Code 2.1.267 |
| `--path` | `~/.claude/settings.json` | settings file to merge into |
| `--dry-run` | off | print the JSON instead of writing |
| `--no-native` | off | hooks only: leave Claude Code's own telemetry off |
| `--replace-endpoint` | off | overwrite an `OTEL_EXPORTER_OTLP_ENDPOINT` already in the file. By default a different one is **kept**, with its protocol, and reported: replacing it silently would redirect Claude Code's telemetry |
| `--native-only` | off | Claude Code's own telemetry only, no hooks. **Use this with the plugin installed**: the plugin registers the hooks, and writing them into settings too makes each one fire twice. Needs no `stdtel-hook` |
| `--collector-confirmed` | off | `detailed-view on` only: the collector is remote and you know it applies this repository's privacy rules |
| `--vscode-settings` | — | `copilot` only: the VS Code `settings.json` to merge into |

Exit codes: `0` success · `1` `stdtel-hook` could not be found (install the package first).

```bash
stdtel-install where                          # /Users/you/.local/bin/stdtel-hook
stdtel-install settings --dry-run             # inspect before writing
stdtel-install settings                       # merge into ~/.claude/settings.json
stdtel-install hooks --harness copilot-cli    # for Copilot's own hook config
```

---

## `stdtel-validate`

The CI contract gate. Validates every `SKILL.md` under a root against the standards contract.

```
stdtel-validate [root] [--quiet]
```

| flag | default | meaning |
|---|---|---|
| `root` | `skills` | directory scanned recursively; follows symlinked directories |
| `--quiet` / `-q` | off | print only the summary and any failures |

| exit | meaning |
|---|---|
| `0` | every manifest valid |
| `1` | a manifest is invalid or two skills share a name |
| `2` | the root does not exist |

> To run an unreleased change, install from a checkout instead: `uv tool install /path/to/skills-telemetry`. See [releasing.md](releasing.md).

Exit `2` matters: this used to exit `0` with "0 skill(s) valid" for a missing directory, so a typo'd
path in CI reported a clean gate over nothing.

```bash
stdtel-validate skills
stdtel-validate ~/.claude/skills -q
```

---

## `stdtel-eval`

Runs the offline with/without-skill evaluation and grades the result with OPA.

```
stdtel-eval [--harness H] [--tasks FILE] [--skills DIR] [--policies DIR] [--out FILE] [--dry-run]
```

| flag | default | meaning |
|---|---|---|
| `--harness` | `claude-code` | `claude-code` or `copilot-cli`; shells out to that CLI |
| `--tasks` | `eval/tasks.yaml` | task definitions (prompt, fixture, skill, policies) |
| `--skills` | `skills` | catalogue root |
| `--policies` | `policies` | directory of Rego packages |
| `--out` | `eval/results.jsonl` | JSONL results, **appended** |
| `--dry-run` | off | see the warning below |

> **`--dry-run` does not evaluate anything.** It skips the harness call *and* returns `True` for every
> policy, so every arm passes unconditionally. It is a pipeline smoke test for CI, not an evaluation,
> and its output must never be read as a result. Use `mise run eval` for a real graded run.

Requires `opa` on PATH for a real run. A policy that cannot be evaluated **fails** and says why on
stderr, rather than passing silently ([ADR-005](adrs/005-data-integrity.md)).

Exit codes: `0` the run completed (this says nothing about whether the arms passed — read
`policy_results` in the output) · non-zero if the policy root is missing or the harness call fails.

---

## `stdtel-policy-report`

Grades a PR's working tree with OPA and writes the `policy_result` rows the primary metric is computed
from. Runs in CI; `warehouse/load_delivery.py --policy-results` ingests the output unchanged.

```
stdtel-policy-report --pr-id owner/repo#42 (--run-seq N | --derive-run-seq REPO WORKFLOW BRANCH)
                     [--policies DIR] [--policy-ids a,b] [--workdir DIR] [--out FILE]
```

| flag | default | meaning |
|---|---|---|
| `--pr-id` | *(required)* | `owner/repo#number`; the GitHub adapter maps it onto `change_request.cr_id` (ADR-013) |
| `--run-seq` | — | explicit sequence number; `1` is the first CI run on the PR |
| `--derive-run-seq` | — | count prior completed runs with `gh` and use the next number |
| `--policies` | `policies` | Rego root |
| `--policy-ids` | *(all discovered)* | comma-separated package names |
| `--workdir` | `.` | tree to grade |
| `--out` | `policy-results.jsonl` | JSONL output |

> **`run_seq` is the metric, not metadata.** First-*time* pass rate cannot be reconstructed later —
> re-runs, retries and cancelled jobs make the true ordering unknowable from history. Record it when
> CI knows it. If the count cannot be determined, the tool treats the run as the first and says so:
> a duplicated `run_seq=1` shows up as a conflict, whereas skipping to `2` would silently destroy the
> first-time measurement.

Exit codes: `0` report written (this says nothing about whether policies passed — read the rows) ·
`2` the policy root is missing or contains no packages.

A policy that cannot be evaluated is written as `passed: false` with the reason on stderr. It is never
omitted, because an absent row and a failing row must not look alike.

---

## `stdtel-load`

Runs every warehouse loader once: `load_traces`, `load_requests`, then (with `STDTEL_LOAD_REPO`)
`fetch_policy_results` and `load_delivery`. It is what a scheduler runs (`stdtel-install loader`),
and you can run it by hand. It needs the warehouse extras: `uv tool install 'stdtel[warehouse]'`.

```
stdtel-load [--dsn DSN] [--tempo URL] [--loki URL]
```

- **One run at a time per user.** An OS lock (`flock`) on `~/.stdtel/loader.lock` (under `STDTEL_HOME`), the scope of the per-user job that runs it. Two users' runs against one warehouse are safe together: every write is idempotent or newest-wins. The kernel grants it
  atomically and releases it when its holder exits, however it exits, so a dead run never blocks the
  next and a live one is never displaced. An overlapping run exits `0` at once and writes nothing: no
  log line, no `loader_run` row. `loader liveness` is what notices runs that never happen.
- **The window reaches back to the watermark.** Each loader loads from its last successful
  `source_max_ts` plus 2 h of overlap, at least 24 h and at most 720 h. A fixed 24 h window could
  never repair an outage longer than a day. Overlapping windows cost nothing: inserts are idempotent.
- **Delivery needs a repository.** Without `STDTEL_LOAD_REPO`, `load_delivery` is recorded as a failed
  run whose error names the setting, so `loader liveness` says what to set rather than staying silent.
- **Output** goes to `~/.stdtel/loader.log`, kept to its last 1 MB before and after each run. The
  terminal gets one summary line.

Exit codes: `0` every loader succeeded, or another run held the lock · `1` any loader failed,
including delivery without `STDTEL_LOAD_REPO` (the others still ran; the log says which) · `2` the
warehouse extras are not installed (`uv tool install 'stdtel[warehouse]'`).

## `stdtel-doctor`

Checks an installation and says what to do about each problem. Every failure mode in this system is
quiet by design — hooks exit 0 so telemetry never blocks you — so a broken install looks exactly like
a working one.

```
stdtel-doctor [--quiet] [--json] [--content-check]
```

Each check has one of four outcomes (ADR-015 decision 1), and only `pass` is a pass:

| outcome | mark | means |
|---|---|---|
| `pass` | `ok` | checked, and well |
| `fail` | `FAIL` | checked, and broken; the remedy says what to do |
| `pending` | `WAIT` | too early to tell, such as an event that has not had a chance to fire |
| `unknown` | `????` | the evidence could not be read, or the check itself raised. Never read as a pass |

| check | not passing means |
|---|---|
| hook resolvable | a hook cannot run `stdtel-hook`; probed under `/bin/sh -c`, not your interactive shell |
| hooks registered | not registered, or registered **twice** (settings *and* plugin), which double-counts every skill window |
| plugin in step | the plugin and the package are different releases. They are separate installs and neither updates the other; a stale plugin keeps working while the skills it ships lag |
| branch identity | no branch readable, or no remote: this work cannot join a change request (ADR-013). Any branch name joins; `main`/`master` passes with a note that work committed straight to it has no change request |
| skill catalogue | skills will record as `unversioned`, with no `standard_id` or `policy_ids` |
| collector reachable | spans are being dropped right now |
| content dropped | `fail`: the collector let content through to Tempo or Loki. `unknown`: the check could not prove it didn't (the probe never arrived, or could not be sent). Probes when any of Claude Code's content settings is on: `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_USER_PROMPTS` or `OTEL_LOG_ASSISTANT_RESPONSES`, in the environment, `~/.claude/settings.json`, or the project's `.claude/settings.json` / `settings.local.json`. `OTEL_LOG_RAW_API_BODIES` on is a `fail` without a probe: a raw body can arrive as a record's body, which no collector attribute rule touches, so nothing here can prove it dropped. With every one off it passes |
| hooks running | `fail`: registered but never fired, or only firing in other projects — hook config is read at session start, so a session open before the install never picks it up. `unknown`: state exists but no Claude Code transcript owns it, so whose it is cannot be told (expected on a Copilot-only machine) |
| artefact events | `SubagentStart`, `SubagentStop` or `PostCompact` has never fired on this machine, so sub-agent and compaction capture is unproven here. This is `pending`, not `fail`, until a session runs with those hooks registered **and** spawns a sub-agent or compacts. Until then those kinds are read from the session directory instead and marked `std.artefact.source=transcript` |
| native coverage | `fail`: a Claude Code session completed a turn more than 10 minutes ago and Loki holds no `api_request` for its session id. Each is named with its repository and start time. **Restart it; resuming is not enough**: a session reads its telemetry settings once, at start. A session is judged by its first completed turn, or by its latest when the first is older than Loki can be asked about (7 days, or Loki's retention if shorter). `pending`: that turn is inside the 10-minute grace period, or no session completed a turn in the last 24 h (nothing to judge is not a pass). `unknown`: Loki unreachable (`STDTEL_LOKI`), or a session with no completed turn inside that reach. Retention is Loki's global `limits_config.retention_period`; `retention_stream` rules and per-tenant overrides are not evaluated. Judges sessions whose state was written in the last 24 h, Claude Code only. ADR-015 decision 2 |
| warehouse freshness | `fail`: a loader's source (Tempo for `load_traces`, Loki for `load_requests`) holds events newer than the warehouse's watermark by more than twice `STDTEL_LOAD_INTERVAL`. The watermark is the newest source timestamp any successful run loaded, so a partial run never advances it. A quiet source with an old warehouse passes. `unknown`: Postgres or the source unreadable, a loader with no successful run yet, or a watermark older than the source can be searched (Tempo's 168 h maximum, Loki's 168 h here), so finding nothing there proves nothing. A watermark within the allowance of now is answered without a query. Quote no warehouse figure while this is not `pass`. ADR-015 decision 3 |
| loader liveness | `fail`: a loader's latest attempted run failed (its error is shown), or one of `load_traces`, `load_requests`, `load_delivery` has not run within twice `STDTEL_LOAD_INTERVAL`, or never ran. Needs only Postgres, so a source outage never hides a failed run. `unknown`: Postgres unreadable, or `psycopg` not installed (`uv tool install 'stdtel[warehouse]'`) |

`--content-check` runs only the content probe, whatever the settings say. It is ADR-014 decision 13:
before the detailed view is switched on, prove on *this* machine that the collector drops content.
It sends one span and one event under `service.name=stdtel-probe`, each holding a random marker in
`tool_input`, `full_command`, `prompt` and `response`, plus a control id (`stdtel.probe.id`) that must arrive.
It then reads Tempo (`STDTEL_TEMPO`, default `http://localhost:3200`) and Loki (`STDTEL_LOKI`,
default `http://localhost:11010`) for up to 30 seconds. It passes only when the control is found
and the marker is not, in both stores. A missing control fails as inconclusive: an empty store
proves nothing, so the outcome is `unknown`. The loaders skip `stdtel-probe`, so a probe never becomes a row.

`--quiet` hides passes. `--json` prints a JSON array, one object per check, and nothing else on
stdout; it ignores `--quiet`, so a reader always sees every check. This is the contract the status
line, the analyst agent and the health page read:

```json
{"name": "artefact events", "outcome": "pending", "observed": "not yet observed: post-compact",
 "remedy": "expected until a session ...", "observed_at": "2026-10-09T09:06:39+00:00"}
```

| exit | when |
|---|---|
| `0` | every check passed |
| `1` | any check failed |
| `2` | none failed, but some are `pending` or `unknown`, or no check ran |

A script gating onboarding on "nothing is broken" accepts `0` and `2`; one gating on "everything is
proven" accepts only `0`.

The branch-identity and catalogue checks matter most early: both are **only fixable while the work is
happening**. A branch renamed tomorrow does not retroactively attribute today's PRs.

---

## `stdtel-statusline`

A status bar line carrying the data-quality faults you can still fix. Wired via the `statusLine`
setting; Claude Code sends session JSON on stdin.

```json
{ "statusLine": { "type": "command", "command": "stdtel-statusline" } }
```

```
stdtel fix/media-hardening-233         everything fine: the local branch name
stdtel ⚠ no branch identity            not a git checkout, or no remote: work here cannot join a PR
stdtel ⚠ 1 unversioned                 a skill is not in the catalogue
stdtel ⚠ spans dropping                 the last export failed
```

Silent when there is nothing worth saying, and when `STDTEL_DISABLED=1` or `STDTEL_STATUSLINE=off`.

**It shows no cost and no token total, deliberately.** The payload offers both. This project commits
to per-skill analysis rather than individual measurement, and a live running total of your own spend
in your editor reads as surveillance however it is framed — a test asserts it stays out.

It reads **local session state only**: no network call, and no catalogue parse unless a skill window
is open. Claude Code debounces at 300ms and cancels an in-flight script, so a slow statusline renders
nothing at all. Measured at ~30ms.

The faults it shows are the ones that cannot be fixed later — a branch renamed tomorrow does not
retroactively attribute today's PRs.

Exit code: always `0`, including on a malformed payload. A statusline that fails must not take the
status bar down with it.

---

## `stdtel-export`

Drains the spool and exports it. Only needed when `STDTEL_SPOOL=1` (ADR-008), where the hook writes
spans to disk and opens no socket.

```
stdtel-export [--once | --watch] [--interval SECONDS]
```

| flag | default | meaning |
|---|---|---|
| `--once` | default | drain and exit; suits cron, launchd, or `make up` |
| `--watch` | off | drain repeatedly |
| `--interval` | `30` | seconds between drains under `--watch` |

**A failed export leaves the spool intact** and exits non-zero, so the next run retries. Records are
removed only after the export returns, and records appended while it runs are kept.

The spool is bounded (`STDTEL_SPOOL_MAX`, default 50,000 records); over the bound the oldest are
dropped **and the count is reported on stderr** — a silent drop at the bound would reintroduce exactly
the invisible loss spooling removes.

---

## `stdtel-conform`

Checks that a foreign emitter's spans meet the external-spend contract (ADR-010). Run it in *their*
CI, not ours.

```
stdtel-conform spans.json        # or: ... | stdtel-conform -
stdtel-conform --print-contract  # the contract itself, as JSON
```

Input is OTLP JSON — what an OTLP/HTTP exporter posts (`resourceSpans`) or what Tempo returns
(`batches`). It checks the span name, that the kind is one of the closed set, that every attribute is
in that kind's allowlist, that no attribute carries content, that an external span names its system,
that a session id (optional — a run outside a Claude session is still real spend) is in the format the
harness exports, and that a cost is a number. A `cost_source` must be `provider` or `local` and must
describe an observed `cost_usd`; an estimate belongs in `cost_estimated_usd`, never under a label.

It also reports **how many external spans carry an observed cost at all**, and how many carry only an
estimate, which is not counted as reported. It says so when most carry none. A shape-only check passes a
file whose costs are missing, and the warehouse then averages over the holes — which is the specific
failure this gate exists to catch.

A run with `std.external.requests_unpriced` above zero is reported as a lower bound (contract v3,
ADR-012), not as a reported cost. An empty `tool_use_id`, or a count that is negative or sent as a
string rather than an integer, is refused.

`--print-contract` prints the span name, the attribute allowlist, the cost-source vocabulary and a
`contract_version` (currently 3), which increases on every change, additions included. It is for an emitter to diff its
own longhand copy against in CI, so drift is detected. It is not a source to generate that copy from:
a copy pulled at build time lets a rename pass straight through.

Exit codes: `0` conforming · `1` at least one violation · `2` the file could not be read or parsed.
An absent cost is not a violation; a null one is, because omitting the attribute is how absence is
recorded.

## `stdtel-hook`

Invoked by the harness, one process per event, reading the payload as JSON on stdin.

```
stdtel-hook {session-start|pre-tool-use|post-tool-use|post-tool-use-failure|
             subagent-start|subagent-stop|post-compact|stop}
```

One subcommand is different: **`scope-close`** is called by a skill, not the harness, and reads no
stdin (doing so would block on an inherited terminal).

```
stdtel-hook scope-close [--session <id>]
```

It closes the container a `telemetry.scope` skill opened (ADR-010), so work done after the skill
finishes is attributed to its own turn rather than still to the skill. The session comes from
`CLAUDE_CODE_SESSION_ID`, which the harness exports to processes the agent spawns, so a skill passes
nothing; `--session` is for callers outside the harness. Closing when no scope is open is **not** an
error, so a loop that ends on a safety gate need not know which ending happened. Exits `1` only when
no session id can be found — the one case where exiting 0 would be a silent no-op.

**Always exits 0**, including on unknown events and internal errors, so telemetry can never block the
developer. Errors go to stderr — which is invisible in most harness UIs, so treat a silent hook as
suspicious and check with `stdtel-install where` and a manual invocation:

```bash
echo '{"session_id":"t","tool_name":"Skill","tool_use_id":"1","tool_input":{"skill":"x"}}' \
  | stdtel-hook pre-tool-use
```

---

## Environment variables

The authoritative list. Everything else that mentions these links here.

| variable | default | read by | meaning |
|---|---|---|---|
| `STDTEL_OTLP_ENDPOINT` | `http://localhost:4318` | exporter | collector base URL. **Use this, not `OTEL_EXPORTER_OTLP_ENDPOINT`** — Claude Code strips `OTEL_*` from every subprocess, so the OTel name cannot reach a hook. `stdtel-install settings` also writes this value into Claude Code's own `env` as `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `STDTEL_OTLP_TIMEOUT` | `2` | exporter | seconds before a flush gives up. Bounds a dead-collector stall; measured 7.34s unbounded |
| `STDTEL_SKILLS_ROOT` | `~/.claude/skills` | hooks | `os.pathsep` list of catalogue roots. Relative entries resolve against `CLAUDE_PROJECT_DIR`; the user directory is always searched last; earliest root wins |
| `STDTEL_AGENTS_ROOT` | `~/.claude/agents` | hooks | `os.pathsep` list of **sub-agent** roots. Same precedence rules as `STDTEL_SKILLS_ROOT`, and relative entries resolve against `CLAUDE_PROJECT_DIR`, so `agents` picks up a project's own directory. Agents are flat `.md` files, not `<name>/SKILL.md`; a `SKILL.md` found here is skipped rather than loaded as an agent. Plugin-shipped agents live in the plugin cache rather than here, so symlink them in the same way the README suggests for skills, or add the plugin's `agents/` directory to this list |
| `STDTEL_SKILLS_OVERLAY` | unset | hooks | `os.pathsep` list of **overlay** roots: front-matter-only stubs that attribute skills **and sub-agents** you do not own, without editing them. Opposite precedence to `STDTEL_SKILLS_ROOT` — an overlay fills only what a skill does not state itself, so upstream wins the day it declares its own value |
| `STDTEL_TEAM` | `unknown` | enrich | owning team, on every span |
| `STDTEL_HARNESS` | `claude-code` | enrich | harness label. **Required in Copilot's hook `env`**, because its snake_case payload is indistinguishable from Claude Code's |
| `STDTEL_HARNESS_MODE` | `agent` | enrich | `agent` / `interactive`, for a fair cross-harness split |
| `STDTEL_HOOK_BIN` | *(searched)* | plugin launcher | absolute path to `stdtel-hook`, overriding the launcher's search |
| `STDTEL_SPOOL` | unset | hooks | `1` writes spans to `~/.stdtel/spool/` instead of exporting inline; `stdtel-export` sends them |
| `STDTEL_SPOOL_MAX` | `50000` | spool | records kept before the oldest are dropped; the drop count is reported, never silent |
| `STDTEL_SPOOL_DIR` | `~/.stdtel/spool` | spool | where spooled spans are written |
| `STDTEL_STATUSLINE` | unset | statusline | `off`/`0`/`false`/`no` hides the status line without disabling telemetry |
| `STDTEL_DISABLED` | unset | hooks | `1`/`true`/`yes`/`on` disables telemetry entirely; checked before the payload is read |
| `STDTEL_STATE_DIR` | `~/.stdtel/sessions` | state | per-session state between hook processes |
| `STDTEL_BRANCH` | *(git)* | enrich | overrides branch detection; `std.branch.hash` is computed from it (ADR-013) |
| `STDTEL_REPO` | *(git)* | enrich | overrides remote detection |
| `STDTEL_TEMPO` | `http://localhost:3200` | doctor, `make load` | Tempo's query API, read by `stdtel-doctor --content-check` and the trace loader |
| `STDTEL_LOKI` | `http://localhost:11010` | doctor, `make load` | Loki's query API, read by `stdtel-doctor --content-check` and by `warehouse.load_requests`, which also takes `STDTEL_DSN` from the environment |
| `STDTEL_LOAD_REPO` | unset | `stdtel-load`, `make load` | `owner/name` whose pull requests and CI policy results `load_delivery` loads. Unset: delivery is recorded as a failed run naming this setting |
| `STDTEL_HOME` | `~/.stdtel` | `stdtel-load` | where `loader.lock` and `loader.log` live |
| `STDTEL_LOAD_INTERVAL` | `900` | doctor | seconds between scheduled loads. `warehouse freshness` allows a source to run ahead of the warehouse by twice this, and `loader liveness` fails when a loader has not run within twice this. Not a positive integer: both checks are `unknown` |
| `STDTEL_DSN` | `postgresql://postgres:stdtel@localhost:5432/stdtel` | doctor, `make load` | the warehouse. `stdtel-doctor` reads `loader_run` from it for `warehouse freshness` and `loader liveness` (needs `stdtel[warehouse]`); the loaders take it as `--dsn` |
| `STDTEL_BIND` | `127.0.0.1` | local stack | interface the compose stack publishes its ports on. `0.0.0.0` exposes an anonymous-admin Grafana and the warehouse Postgres to your network — only on one you trust |
| `CLAUDE_PROJECT_DIR` | *(cwd)* | hooks | set by the harness; the base for relative skills roots |
| `CLAUDE_CODE_SESSION_ID` | *(unset)* | `scope-close` | set by the harness and **exported to processes the agent spawns**, so a skill can run `stdtel-hook scope-close` without knowing which session it is in. A sub-agent's shell sees the *parent* session's id, alongside `CLAUDE_CODE_CHILD_SESSION` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | exporter, doctor | for stdtel's exporter, a fallback for direct CLI/CI use only, where nothing scrubs it. In Claude Code's settings `env` (written by `stdtel-install settings`) it is where **Claude Code's own** telemetry goes, and the endpoint `stdtel-doctor --content-check` and `detailed-view on` probe |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | — | exporter | fallback, checked before the base URL; must be the full `/v1/traces` URL |
| `OTEL_SERVICE_NAME` | `stdtel` | exporter | `service.name` on the resource |
| `OTEL_LOG_TOOL_DETAILS` | unset | doctor | **Claude Code's** setting, not stdtel's: it makes Claude Code export tool inputs and commands. stdtel only reads it, from the environment and Claude Code's settings files, to decide whether `stdtel-doctor` must prove the collector drops content. Never set it before `stdtel-doctor --content-check` passes |

`STDTEL_DSN` is read by `stdtel-doctor`; the loaders take the DSN as `--dsn`, which `make load` fills from it.
