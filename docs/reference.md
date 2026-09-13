# CLI reference

Four console scripts. `stdtel-hook` is invoked by the harness, never by you; the other three are yours.

All of them read the [environment variables](#environment-variables) below.

---

## `stdtel-install`

Renders hook configuration bound to **this** installation's absolute path, and merges it into a
settings file. Run it after installing the package or the plugin — a distributed hook manifest carries
the bare command name and is deliberately one step short of working, because it cannot know your path.

```
stdtel-install where
stdtel-install hooks    [--harness claude-code|copilot-vscode|copilot-cli] [--exec-form]
stdtel-install settings [--path PATH] [--exec-form] [--dry-run]
```

| subcommand | does |
|---|---|
| `where` | prints the absolute `stdtel-hook` path it will write. Start here when hooks silently do nothing |
| `hooks` | prints the hook JSON for a harness to stdout; pipe it wherever you keep config |
| `settings` | merges the Claude Code hook block into a settings file, preserving everything already there |

| flag | default | meaning |
|---|---|---|
| `--harness` | `claude-code` | which dialect to emit. The Copilot forms set `STDTEL_HARNESS` per hook, which is **required** — see [ADR-003](adrs/003-hook-execution-constraints.md) |
| `--exec-form` | off | emit `command` + `args` instead of a shell string. Saves ~3ms per call by skipping `sh -c`; verified working against Claude Code 2.1.267 |
| `--path` | `~/.claude/settings.json` | settings file to merge into |
| `--dry-run` | off | print the JSON instead of writing |

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
| `--pr-id` | *(required)* | must match `pull_request.pr_id`, i.e. `owner/repo#number` |
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

## `stdtel-doctor`

Checks an installation and says what to do about each problem. Every failure mode in this system is
quiet by design — hooks exit 0 so telemetry never blocks you — so a broken install looks exactly like
a working one.

```
stdtel-doctor [--quiet]
```

| check | failing means |
|---|---|
| hook resolvable | a hook cannot run `stdtel-hook`; probed under `/bin/sh -c`, not your interactive shell |
| hooks registered | not registered, or registered **twice** (settings *and* plugin), which double-counts every skill window |
| ticket key | this branch yields `unattributed`, so the work is excluded from outcome analysis |
| skill catalogue | skills will record as `unversioned`, with no `standard_id` or `policy_ids` |
| collector reachable | spans are being dropped right now |
| hooks running | registered but never fired — no session state has been written |

`--quiet` shows only problems. Exit `0` when everything passes, `1` otherwise, so it can gate
onboarding.

The ticket-key and catalogue checks matter most early: both are **only fixable while the work is
happening**. A branch renamed tomorrow does not retroactively attribute today's PRs.

---

## `stdtel-statusline`

A status bar line carrying the data-quality faults you can still fix. Wired via the `statusLine`
setting; Claude Code sends session JSON on stdin.

```json
{ "statusLine": { "type": "command", "command": "stdtel-statusline" } }
```

```
stdtel STDTEL-16                        everything fine
stdtel ⚠ no ticket                      this branch yields no ticket key
stdtel ⚠ no ticket · 1 unversioned      ...and a skill is not in the catalogue
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

## `stdtel-hook`

Invoked by the harness, one process per event, reading the payload as JSON on stdin.

```
stdtel-hook {session-start|pre-tool-use|post-tool-use|post-tool-use-failure|stop}
```

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
| `STDTEL_OTLP_ENDPOINT` | `http://localhost:4318` | exporter | collector base URL. **Use this, not `OTEL_EXPORTER_OTLP_ENDPOINT`** — Claude Code strips `OTEL_*` from every subprocess, so the OTel name cannot reach a hook |
| `STDTEL_OTLP_TIMEOUT` | `2` | exporter | seconds before a flush gives up. Bounds a dead-collector stall; measured 7.34s unbounded |
| `STDTEL_SKILLS_ROOT` | `~/.claude/skills` | hooks | `os.pathsep` list of catalogue roots. Relative entries resolve against `CLAUDE_PROJECT_DIR`; the user directory is always searched last; earliest root wins |
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
| `STDTEL_BRANCH` | *(git)* | enrich | overrides branch detection; the ticket key is parsed from it |
| `STDTEL_REPO` | *(git)* | enrich | overrides remote detection |
| `STDTEL_BIND` | `127.0.0.1` | local stack | interface the compose stack publishes its ports on. `0.0.0.0` exposes an anonymous-admin Grafana and the warehouse Postgres to your network — only on one you trust |
| `CLAUDE_PROJECT_DIR` | *(cwd)* | hooks | set by the harness; the base for relative skills roots |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | exporter | fallback for direct CLI/CI use only, where nothing scrubs it |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | — | exporter | fallback, checked before the base URL; must be the full `/v1/traces` URL |
| `OTEL_SERVICE_NAME` | `stdtel` | exporter | `service.name` on the resource |

`STDTEL_DSN` is not read by any code — it is a convention used in the docs for
`postgresql://postgres:stdtel@localhost:5432/stdtel`.
