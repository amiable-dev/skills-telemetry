# standards-telemetry

Telemetry and metadata capture for **standards-as-skills** — attributes token cost and outcomes to
individual skills across Claude Code and GitHub Copilot, joined to policy (OPA/Rego), delivery
(Linear/GitHub) and quality data. Design rationale: [docs/design-proposal.md](docs/design-proposal.md).

Metadata only. No prompt, response or file content is ever emitted; the collector drops it again as defence in depth.

## Layout

```
skills/<name>/SKILL.md      skill catalogue with validated front-matter (the contract)
stdtel/manifest.py          front-matter parser + `stdtel-validate` CI gate
stdtel/hooks/cli.py         Claude Code hooks: session-start | pre-tool-use | post-tool-use | stop
stdtel/transcript.py        incremental JSONL reader + token attribution (tail rule, first-only sensitivity)
stdtel/exporter.py          std.skill.invocation spans via OTLP/HTTP (content scrubbed)
stdtel/enrich.py            join keys: std.ticket.id from branch, std.repo, std.team, std.harness
stdtel/skillmap.py          generates collector/copilot-skill-map.yaml for Copilot tool-call mapping
collector/otel-collector.yaml  drop content → normalise gen_ai.* → map Copilot skills → pseudonymise → spanmetrics
deploy/docker-compose.yml   collector + Tempo + Prometheus + Grafana (provisioned scorecard) + Postgres
warehouse/schema.sql        skill_invocation, session_cost, ticket, pull_request, policy_result, defect, skill_eval
warehouse/scorecard.sql     weekly per-skill scorecard → keep / refine / review-merge / deprecate
warehouse/load_traces.py    Tempo → Postgres loader
eval/run_eval.py            offline with/without-skill eval, OPA as grader (`--dry-run` for CI)
.claude/settings.json       hook + OTel env wiring for Claude Code
```

## Quick start

```bash
make install && make test          # 15 tests, in-memory OTel exporter
make validate                      # front-matter contract gate (fails CI on bad SKILL.md)
make up                            # local stack; Grafana on :3000, Tempo :3200, Prometheus :9090
cp .claude/settings.json ~/.claude/settings.json   # or merge into a repo-level settings file
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
claude                             # use a skill; watch std.skill.invocation spans arrive in Tempo
```

Copilot: enable managed OTel export (VS Code / CLI) pointing at the same collector with resource attributes
`std.harness=copilot-vscode`, `std.team=<team>`; `collector/otel-collector.yaml` maps catalogued skill tool-calls onto `std.skill.*`.

## Span schema (`std.skill.invocation`)

| attribute | source |
|---|---|
| `std.skill.name/version/trigger`, `std.standard_id`, `std.policy.ids` | hook + manifest |
| `std.skill.load_tokens`, `std.skill.tail_tokens`, `std.skill.tail_tokens_first_only`, `std.skill.llm_requests` | transcript attribution |
| `gen_ai.usage.{input,output,cache_read_input,cache_creation_input}_tokens`, `gen_ai.request.model` | transcript |
| `std.ticket.id`, `std.repo`, `std.team`, `std.harness`, `std.harness.mode` | resource (SessionStart) |
| `std.user.hash` | collector (pseudonymised) |

## Known limitations

- Skill name is parsed from the Skill tool input in hooks — no first-class field yet (tracked upstream). Parsing is isolated in `hooks/cli.py::_skill_from_input`.
- Tail attribution splits by load order; when several skills load in one turn compare against `tail_tokens_first_only`.
- Copilot granularity is per turn; use Claude Code's finer data for within-harness tuning only.
- `load_tokens` uses a chars/4 heuristic on the Skill tool result.
- `gen_ai.*` conventions are still *Development* upstream; extend `transform/normalise` as names move.
