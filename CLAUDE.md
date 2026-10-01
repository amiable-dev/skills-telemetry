# skills-telemetry — project context for Claude Code

## What this is
Telemetry and metadata capture for **standards-as-skills**: attribute token cost and outcomes to individual
skills across Claude Code and GitHub Copilot, joined to OPA/Rego policy results, delivery data (Linear/GitHub)
and quality signals. Purpose: a data-driven keep / refine / merge / deprecate loop for skills, and a fair
Claude Code vs Copilot (VS Code) comparison. Full rationale and industry scan: `docs/design-proposal.md`.

Public repo: `github.com/amiable-dev/skills-telemetry`. **Nothing stored or forwarded past the collector ever
holds content** (prompts, responses, file contents, command lines, tool inputs or results). stdtel's own
hooks and exporter emit none at all. A harness sends content to the collector only by the user's explicit,
informed opt-in through `stdtel-setup`, once the collector has proven on that machine that it drops it
([ADR-014](docs/adrs/014-harness-native-telemetry.md) decision 14; this replaced "metadata only — never emit
prompt/response/file content").

## Key design decisions (do not revisit without reading the ADR)
Decisions live in `docs/adrs/`. These bullets are pointers, not the reasoning — the ADR records what
was rejected and what the decision costs, which is what you need before re-litigating it.

- **[ADR-001](docs/adrs/001-distribution-and-capture-surface.md)** — distribution: one repo for three
  loaders, hooks vendor-specific, `uv tool install` + absolute path. Copilot instrumented by native
  OTel config, not our hooks.
- **[ADR-002](docs/adrs/002-delivery-data-joins-and-proxies.md)** — delivery data: ~~join on the branch
  ticket key~~ (superseded by ADR-013); unlabelled PR is `unknown` never `none`; `review_rounds` counts CHANGES_REQUESTED;
  `policy_result` from a CI artefact because `run_seq` is unrecoverable; cycle time is a labelled proxy.
- **[ADR-003](docs/adrs/003-hook-execution-constraints.md)** — the harness environment: `OTEL_*` is
  scrubbed so config is `STDTEL_*`; the flush timeout must sit on the exporter; hooks get a non-login
  `sh -c` so paths are absolute; module scope stays stdlib-only; camelCase proves Copilot.
- **[ADR-004](docs/adrs/004-skill-identity-and-catalogue.md)** — identity: contract fields under
  `metadata:`; catalogue keyed on the bare name with `plugin:skill` resolved by suffix; hooks lenient,
  CI strict; the scanner follows symlinks.
- **[ADR-005](docs/adrs/005-data-integrity.md)** — never record an unobserved value; a component that
  cannot do its job fails loudly; vacuous truth is a bug; missing data is its own category.
- **[ADR-008](docs/adrs/008-spool-spans-to-disk.md)** (proposed) — `stop` should append NDJSON to a
  spool and never open a socket; a separate process drains it. Auto-starting the stack was rejected:
  the collector is shared infrastructure, and the coupling is the problem, not the symptom. Issue #14.
- **[ADR-009](docs/adrs/009-artefact-activation-as-the-unit-of-capture.md)** — the unit of capture is
  an artefact activation (`std.artefact.kind` ∈ skill, subagent, compaction, turn), so efficiency is
  answerable per session at any volume. `std.skill.invocation` was renamed to
  `std.artefact.activation`; compatibility lives in the loader, which still writes `skill_invocation`
  from kind `skill`. Per-kind attribute allowlists in `stdtel/artefact.py` are what make one span
  name safe. A turn carries no `name` — `prompt_id` is unbounded and `name` is a metrics dimension.
  Turn spans are deltas: count `DISTINCT prompt_id`, never `count(*)`. Issue #63.
- **[ADR-010](docs/adrs/010-containment-and-scope.md)** (proposed) — containment: activations are
  children of their **turn**, never of the session (a session is resumed and persists — one real one
  spans seven weeks, which no trace store will hold). Across turns, containment is carried by
  `std.scope.name` / `std.scope.key` / `std.scope.id` attributes and rolled up in SQL, not by a span
  tree. Span links were rejected on evidence: Langfuse ignores them and TraceQL evaluates one trace at
  a time, so a link can be filtered but never joined. **Self cost is stored; inclusive cost is
  derived** — no row holds a total including its children. A fifth kind `external` carries spend the
  harness cannot see. A rollup answers containment, never causation. Issues #75, #77.
- **[ADR-011](docs/adrs/011-decorating-artefacts.md)** (proposed) — how artefacts declare scope, and
  the rule that **nothing is required of any author**: undeclared means turn-scoped, never a gate
  (ADR-004's `policy_ids` gate is the scar). Skills use `telemetry.scope` under `metadata:`.
  Sub-agents accept a `metadata:` block by *tolerance*, not contract — verified 2026-09-21 on 2.1.277,
  undocumented, could regress silently. MCP servers cannot be decorated at all and are overlay-only.
  The overlay is the universal fallback and its declarations are assertions, not observations, so they
  need provenance. Scope of work includes the user documentation and widening `stdtel-onboard`.
- **[ADR-012](docs/adrs/012-external-contract-v3.md)** — external contract v3. An MCP
  emitter's `session.id` goes stale after `/clear` (the server reads it once at launch; verified
  2026-09-28), and no session id arrives per call. The fix is a join: the emitter sends the
  `_meta["claudecode/toolUseId"]` it receives, and the Stop hook records `mcp__*` tool-use ids from the
  transcript. Joined at read time, never overwriting what the emitter sent, so external spend gains a turn
  and a scope. Adds `requests_unpriced` so a partly priced run keeps its observed cost as a lower bound.
  Issue #94.
- **[ADR-013](docs/adrs/013-join-work-to-change-requests.md)** — replaces ADR-002
  decision 1. Work joins to change requests on a hash of repo + branch plus a time window, confirmed by
  commit patch-ids, because the ticket-from-branch regex needed a naming convention nobody was told
  about and invented fake tickets (`RELEASE-0`, `ANALYZE-4`). Delivery data loads through a
  forge-neutral change-request record, with a GitHub adapter now and GitLab in #103. The ticket becomes
  enrichment: forge issue links first, then a fixed regex. Issue #102.
- **[ADR-014](docs/adrs/014-harness-native-telemetry.md)** — both harnesses now measure
  what stdtel estimated. Claude Code's `claude_code.api_request` events carry `skill.name`,
  `agent.name`, `plugin.name`, `mcp_server.name`, `cost_usd` and `prompt.id` per request. Copilot's
  native OTel has `github.copilot.tool.parameters.skill_name` and `github.copilot.git.*`, but no
  per-skill tokens and no cost. Consume native records into one `llm_request` table (Copilot per-skill
  attribution derived and labelled), keep stdtel for the join, the standard, the outcome, external
  spend and containment. Native metrics stay out of Prometheus (`session.id` is a label on each; #92).
  Verified live: native `prompt.id` equals the transcript's `promptId`, and identity is on every
  record. `OTEL_LOG_TOOL_DETAILS` (names third-party skills, but exports content to the collector) is
  recommended through a new `stdtel-setup` skill: explained, enabled only on the user's yes, and only
  after a doctor check proves the collector drops a content marker. The "metadata only" rule is now
  "nothing past the collector ever holds content" (decision 14, applied 2026-09-30). Blocks on the collector first: no record-level pseudonymising, events stored
  nowhere. Survey and sources: `docs/landscape.md`. Issue #107.
- **[ADR-007](docs/adrs/007-plugin-evals-and-what-each-eval-measures.md)** (proposed) — two things are
  called "eval": `eval/run_eval.py` grades policy outcomes with OPA (deterministic); `claude plugin
  eval` grades Claude's behaviour on a prompt (not). Our suite verifies no behaviour at all — skill and
  agent claims are string checks over markdown. Issue #10.
- **[ADR-006](docs/adrs/006-langfuse-as-an-optional-trace-backend.md)** — Langfuse is an optional extra
  trace exporter behind a collector overlay, not a replacement: it cannot capture, and its unit is the
  trace while the primary metric joins at PR grain. `std.*` must be duplicated into
  `langfuse.trace.metadata.*` or it arrives unqueryable.

Still true and not yet an ADR:
- Primary effectiveness metric = **first-time OPA policy pass rate** on the PR, with vs without the skill.
- Custom attributes live in the `std.*` namespace; `gen_ai.*` is normalised at the collector (still
  Development, now in `open-telemetry/semantic-conventions-genai`; extend `transform/normalise`).
- Token attribution: "tail" rule — llm requests after a skill loads until the next skill loads / turn
  ends; `tail_tokens_first_only` kept as a sensitivity check.
- Two span types at Stop: `std.session.cost` (total, emitted even with no skill) and
  `std.artefact.activation` with `kind=skill` (a share of it). They overlap deliberately — never
  sum them. The other three kinds (`subagent`, `compaction`, `turn`) are ADR-009; a turn's tokens and
  a skill's tail overlap the same way.
- `PostToolUse`/`PostToolUseFailure` are unmatched (every tool); `PreToolUse` stays matched to Skill.
- `SessionState.save()` must list every field — each hook is a separate process, so an unpersisted
  field reads as its default at the next event, silently.
- Copilot skills arrive as generic tool-call spans; `collector/copilot-skill-map.yaml` (generated by
  `make skill-map`, test-enforced) maps them onto `std.skill.*`.

## Layout
`stdtel/` (manifest, state, transcript, exporter, enrich, skillmap, hooks/cli) · `skills/` catalogue (incl. `stdtel-onboard`) · `agents/` scorecard analyst ·
`docs/adrs/` decisions · `collector/` OTel config · `examples/` global vs per-project settings · `deploy/` compose stack (collector, Tempo, Prometheus, Grafana, Postgres) ·
`warehouse/` schema + weekly scorecard SQL + Tempo→Postgres loader · `policies/` Rego (the primary metric) ·
`eval/` with/without-skill runner + `fixtures/`, graded by real OPA · `tests/` (run `mise run test`; includes replay of real captured hook payloads).

## Docs
`docs/for-developers.md` (what is collected + opt-out) · `docs/reference.md` (CLIs + the authoritative env-var table) · `docs/skills.md` (when to use each
skill/agent) · `docs/evaluation-power.md` · `docs/insight-walkthroughs.md` · `docs/local-stack.md` (endpoints + `mise run smoke`) · `docs/landscape.md` (what else exists, with sources) · `docs/adrs/`.
Adding a console script or a skill without documenting it fails `tests/test_docs_coverage.py`.

## Commands
`stdtel-install settings|hooks|where` binds hooks to an absolute path (see ADR-001).
`mise run install|test|validate|validate-plugin|skill-map|up|up-langfuse|down|smoke|load|load-watch|demo|demo-clear|eval|eval-dry|power|power-check|ci` — mise owns the toolchain (Python 3.13)
and auto-activates `.venv`; the tasks delegate to the Makefile, which stays the single definition.
The venv is seeded with pip on purpose: mise creates it with uv, which omits pip, and a pip-less venv
sends a bare `pip install` to the interpreter behind it instead.

## Reporting thresholds
Phases are set by data volume, not elapsed time; the hard floor and the three phases live in README
and are repeated verbatim in `agents/skill-scorecard-analyst.md` and `skills/stdtel-query`. The
canonical sentence is `eval.power.HARD_FLOOR` and a test asserts it appears identically in all four
surfaces — do not reword it in one place. Derivations: `docs/evaluation-power.md`; worked examples with real captured output, including
the misreadings this data invites: `docs/insight-walkthroughs.md`.

## Current state / known gaps
- `make demo` loads a synthetic fleet (all `DEMO-`/`demo-` prefixed) so the scorecard has something to
  show. It is what first ran `scorecard.sql` against rows, which found #23 — a cartesian join
  inflating `n_with` ~76x and a `keep` verdict for a skill with no outcome data. Both fixed; the
  scorecard now aggregates per PR and returns `insufficient-data` below the floor.
- **Per-request cost is native now (ADR-014, 0.8.0).** `llm_request` is loaded from Claude Code's
  `api_request` events in Loki by `warehouse/load_requests.py`. Read per-skill figures from
  `llm_request_attributed`, which names `"third-party"` requests from stdtel's own activation
  (`attribution_source = derived`). `session_cost` is reconciliation only, and the two are never
  added together. `load_tokens` (chars/4) and the tail rule remain until the release after 0.8.0 (#117).
- Live-verified on Claude Code 2.1.285, 2026-09-30:
  - `api_request` carries the real `skill_name` (`probe-echo`), while `skill_activated` redacts it
    to `custom_skill`;
  - Loki flattens attribute dots to underscores (`skill.name` is stored as `skill_name`);
  - only `service_name` is an index label; the rest is structured metadata. Query with
    `X-Loki-Response-Encoding-Flags: categorize-labels`;
  - native `prompt_id` bridges to the stdtel turn.
- `stdtel-install settings` **enables Claude Code's own telemetry** (`--no-native` opts out).
  `OTEL_LOG_TOOL_DETAILS` is only ever set by `stdtel-install detailed-view on`. That command
  refuses unless the endpoint Claude Code will use is local (or `--collector-confirmed`) and the
  content probe passes. It has not yet been run on this machine.
- Langfuse v4 writes the `events_*` model while `GET /api/public/traces` reads the legacy tables, so
  that endpoint reads empty even when ingestion worked. Query `events_core` to confirm.
- Documented but not yet seen in a live trace (ADR-014): Copilot's `github.copilot.tool.parameters.skill_name` / `github.copilot.git.*` span attributes, and its
  PascalCase compatibility mode. microsoft/vscode#326254 (spans carry content despite
  `captureContent:false`) means metadata-only must be enforced collector-side, not by config.

## Next steps (agreed)
1. **#115 Copilot loader**: blocked on a live Copilot trace. It verifies `github.copilot.tool.parameters.skill_name`
   and `github.copilot.git.*` (documented, not yet seen), then retires `copilot-skill-map.yaml`
   (ADR-014 d11) and loads Copilot `chat` spans into `llm_request` as `derived`.
2. **#117, second half, due in the release after 0.8.0**: remove the tail rule,
   `tail_tokens_first_only` and chars/4 `load_tokens`. Point `stdtel-query` and the scorecard analyst
   at `llm_request_attributed` (both need a version bump and a skill-map regen).
3. Make `--exec-form` the default (now proven to work) and drop the shell-form fallback.
4. Onboard a real project end to end (#21). Since ADR-013 any branch name joins; it needs a real repo
   with a remote, PRs, and the CI policy artefact.

## Done
- **ADR-014 implemented, 0.8.0 (2026-09-30):**
  - collector privacy on every pipeline, for both harnesses (#109), including repository-URL
    credentials (#128);
  - Loki for events, with native metrics dropped (#110);
  - the `stdtel-doctor` content probe (#111);
  - `llm_request` plus its views (#112);
  - native settings and `stdtel-install copilot` (#113);
  - the `stdtel-setup` skill and a gated detailed view (#114);
  - coding-agent PRs kept, and `claude-code-assisted` read as evidence (#116);
  - third-party naming (#117, first half).

  Also fixed: the compose loader had never run `load_traces` (#121), and an empty branch hash was
  stored as `''` (#122).
- **Hooks validated against a live Claude Code session (2.1.267) on 2026-09-10.** Settled both open
  questions: the Skill input field is `skill`; **`caller` is absent from the hook payload**, so the
  transcript fallback is required, not defensive (a real run produced `std.skill.trigger=direct` via
  that path). **Exec form (`command`+`args`) fires correctly.** Real payloads are committed as
  `tests/fixtures/hook_payloads.json` and replayed by `tests/test_live_payloads.py`.
- Payload fields we were discarding are now captured: `std.prompt.id` (joins to native `claude_code.*`
  telemetry), `std.harness.permission_mode` (measured, not the env's guess), `std.skill.duration_ms`
  (the harness's own timing). Full chain re-verified end to end into Tempo from a real session.
- Artifacts: `skills/stdtel-onboard` (decorates a SKILL.md to the contract) and
  `agents/skill-scorecard-analyst` (keep/refine/merge/deprecate from the data, with this dataset's
  traps written down). `policies/telemetry/manifest_valid.rego` makes onboarding *measurable* rather
  than advisory — and note it passed vacuously until `.md` became gradeable.
- **Full chain verified 2026-09-10**: hook -> collector -> Tempo -> Postgres, plus spanmetrics into
  Prometheus with `std_skill_plugin` as a dimension. Three bugs in `warehouse/load_traces.py` that had
  never run: Tempo's `/api/traces/{id}` returns OTLP JSON (`spanId`/`traceId`, base64) not the search
  API's hex `spanID`/`traceID`, and `durationNanos` does not exist there (`ended_at` was always
  `started_at`). `make up`/`down` now detect `docker compose` vs standalone `docker-compose`.
- `policies/logging/{required_fields,no_pii}.rego` — the primary metric computes for real. Before this,
  `grade()` only ran under `--dry-run`, which returns True for every policy, so the headline number was
  a stub. `no_pii` grades bracket-matched log calls extracted by the runner, because real log calls span
  several lines and a line-bound regex missed exactly the multi-line leaks.
- `std.skill.invoked_as` / `std.skill.plugin` wired through schema, loader and spanmetrics; a contract
  test now fails if loader columns, `parse_span` and the schema drift apart.
- Six defects found by auditing against real transcripts + docs, all fixed with regression tests in
  `tests/test_regressions.py`: empty `transcript_path` killed telemetry silently; endpoint never reached
  the exporter; 7.3s stall per turn on a dead collector; Copilot events mislabelled `claude-code`;
  namespaced skills always `unversioned`; `std.skill.trigger` a constant.
- Catalogue roots: `STDTEL_SKILLS_ROOT` takes an `os.pathsep` list, relative entries resolve against
  `CLAUDE_PROJECT_DIR`, `~/.claude/skills` is always searched last, earliest root wins. Hooks load the
  catalogue leniently (`load_catalogue(..., strict=False)`) so one bad SKILL.md cannot silence the rest;
  `stdtel-validate` stays strict. The walker follows symlinked directories — `rglob` did not, which
  silently broke the `ln -s` into `~/.claude/skills` workflow the README recommends.
- README quick start split into "install once globally" vs "onboard a project", with the settings
  precedence table; `.claude/settings.json` is now env-only (hooks live in the global file, duplicating
  them double-fires each hook).
