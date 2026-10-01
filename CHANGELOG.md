# Changelog

Versions are shared by the Python package and the plugin manifests, and a test asserts they agree.
**A version bump is what makes clients pick up a new copy** — both marketplaces serve the cached
version until this number changes — so bump it for anything a user would receive.

## Unreleased

### Added
- **`stdtel-install settings --native-only` (#134).** It writes Claude Code's own telemetry settings
  and no hooks. A plugin install needs exactly that: the plugin registers the hooks, and 0.8.0's
  `settings` wrote them into the settings file as well, so each hook fired twice. The command needs no
  `stdtel-hook`, and it cannot be combined with `--no-native`. The `stdtel-setup` skill (1.1.0) and
  the README now send plugin users to it.

## 0.8.0 — 2026-09-30

Harness-native telemetry, ADR-014. Claude Code's own per-request records become the source of cost
and tokens, loaded into `llm_request`. **`stdtel-install settings` now switches Claude Code's own
telemetry on as well as registering the hooks.** Use `--no-native` for hooks only.
[docs/for-developers.md](docs/for-developers.md) says what that sends and how to stop it. Nothing on
the wire or in the warehouse breaks: every schema change is additive.

### Changed
- **The collector enforces privacy on every pipeline, for both harnesses (ADR-014 decision 3, #109).**
  It had to be done before any native telemetry is switched on.
  - **Identity** is removed from spans, events and metric data points. The email becomes
    `std.user.hash`; account and organisation ids are deleted. Verified live, Claude Code puts these
    on every record rather than on the resource, so the old resource-level rule would not have seen
    them.
  - **Content** is deleted for both harnesses' native keys: Claude Code's `tool_input`,
    `tool_parameters`, `full_command`, `prompt`, `response` and similar; Copilot's
    `github.copilot.tool.parameters.command` and `.file_path`, and its GenAI message and tool-call
    keys.
  - **Copilot's git remote and branch** become `std.branch.hash`, computed exactly as the capture
    side computes it, and the plain values are deleted. A remote URL can carry a token.
  - All of this is tested against the pinned collector image, under both the default and the
    Langfuse configuration. Before this change, 22 of those 24 checks failed.

### Added
- **Third-party skills are named from stdtel's own record (ADR-014 decision 10, #117).** Without the
  detailed view, Claude Code names a third-party plugin's skill `"third-party"`. The new view
  `llm_request_attributed` names such a request from the one skill that stdtel saw run on the same
  prompt and that the harness did not name itself, labelled `derived`. With more than one candidate
  it stays `"third-party"`. It is read-time, so loader order cannot matter. Removing the tail rule and
  chars/4 `load_tokens` remains scheduled for one release after native records are verified live.
- **`skills/stdtel-setup`: interactive setup for both harnesses (ADR-014 decision 12, #114).** An
  agent runs it with the user.
  - It installs the hooks and switches on Claude Code's and Copilot's own telemetry without content.
  - It offers Claude Code's detailed view and says plainly what it costs: shell commands, file paths
    and written file contents travel to the collector and are deleted there.
  - It enables the view only on an explicit yes, through the new
    **`stdtel-install detailed-view on`**. That command enforces the rest in code: the collector must
    be local or vouched for, and the content probe must pass on the spot. Otherwise it writes nothing.
    `off` removes the flag.
  - The gate and `stdtel-doctor`'s content check now probe **the endpoint Claude Code will send
    to**, read from its settings files in its own precedence order (project local, then project,
    then user). Until now they probed stdtel's own endpoint. That is usually the same collector, but
    not after a hand edit, a reinstall with another endpoint, or a project override, and then the
    check proved something about a collector Claude Code would never use.
- **`stdtel-install` switches on both harnesses' own telemetry (ADR-014 decision 2, #113).**
  - `settings` now writes Claude Code's `env` block alongside the hooks: events on, exported to the
    collector stdtel already uses, with repository attributes on and account uuid off.
  - It never writes `OTEL_LOG_TOOL_DETAILS` or a metrics exporter. A detailed view the user already
    chose survives a reinstall. `--no-native` writes the hooks only.
  - New `stdtel-install copilot` prints, or merges with `--vscode-settings`, the VS Code
    `github.copilot.chat.otel.*` settings with `captureContent: false`, and the Copilot CLI's
    `COPILOT_OTEL_*` variables. The keys come from the VS Code documentation; they have not yet been
    seen in a live trace (#115).
  - `docs/for-developers.md` now says what Claude Code sends and how to stop it: `STDTEL_DISABLED`
    does not. It also drops two stale ticket references left over from ADR-013.
- **One table of model requests, loaded from the harness's own records (ADR-014 decisions 5 and 7,
  #112).** `warehouse/load_requests.py` reads Claude Code's `api_request` events from Loki into
  `llm_request`: tokens, exact cost, model, and the skill, agent, plugin and MCP server each request
  served. A live capture on Claude Code 2.1.285 confirmed that `api_request` carries the real skill
  name (`probe-echo`) even where `skill_activated` redacts it. That capture, scrubbed, is the
  replayed fixture.
  - `llm_request_change_request` joins each request to its change request through the stdtel turn
    with the same `prompt_id`, reusing `activation_change_request` rather than restating ADR-013's
    rule. Only turns bridge: a sub-agent in its own worktree shares the prompt id but not the
    branch.
  - `session_cost_reconciliation` compares the harness's session total with the sum of its
    requests. The two are never added together.
  - Cost is stored as unbounded `NUMERIC`. The harness sends seven decimals and more, which
    `NUMERIC(12,4)` would have rounded.
  - Loki's `query_range` stops at its limit silently, so the loader pages until a short page comes
    back. A request with no id fails the run and is counted, not dropped.
  - `make load` and the compose `loader` service now run it after the traces load. `make load` is
    an `&&` chain, so a Loki outage stops the delivery half there, exactly as a Tempo outage
    already did.
- **`stdtel-doctor` proves the collector drops content (ADR-014 decision 13, #111).** A new
  `content dropped` check sends a probe span and event holding a marker in content-shaped keys and a
  control id, then reads Tempo and Loki back. It passes only when the control arrived and the
  marker did not. "Not found" alone would also pass when nothing arrived, so a missing control
  fails as inconclusive. It runs by default only when `OTEL_LOG_TOOL_DETAILS` is on;
  `stdtel-doctor --content-check` runs it on demand and is what `stdtel-setup` will call before
  offering the detailed view. The trace loader ignores `service.name=stdtel-probe`.
- **Loki stores harness-native events (ADR-014 decision 4, #110).** Claude Code's per-request cost,
  tokens and skill attribution exist only as events, and the logs pipeline used to export them to
  `debug` and nowhere else. Loki runs on host port 11010, this project's berth extra, and Grafana
  has it as a datasource. Native *metrics* are dropped at the collector: each carries `session.id`
  as a label, so it opens a series per session.

### Fixed
- **Coding-agent PRs were being discarded as bots; they are now agent work for their harness
  (ADR-014 decision 9, #116).** The fix was verified with the loader's own `gh pr list` call on real
  public PRs:
  - the Copilot coding agent is `app/copilot-swe-agent` and the Claude GitHub app is `app/claude`,
    both `is_bot: true`, so the #62 filter dropped every one;
  - both are now kept, including the `gh search` spellings `Copilot` and `claude[bot]`;
  - dependency bots are still excluded.

  `assisted_by` now combines every signal: the arm labels, Anthropic's `claude-code-assisted` label,
  and an agent author.
  - One harness evidenced is that harness, even under `no-ai`, because a control an agent touched is
    not a control.
  - Two harnesses is `mixed`.
  - The absence of evidence is still `unknown`, never `none`.
- **A credential in Claude Code's repository URL no longer reaches Loki (#128).** With
  `OTEL_METRICS_INCLUDE_REPOSITORY` on, every native event carries `vcs.repository.url.full`, and a
  remote such as `https://x-access-token:...@github.com/...` would have been stored verbatim. The
  collector now strips the userinfo, on the resource and on the record, for every signal. It is
  tested against the real image under both overlays.
- **A session with no repository now has `branch_hash` NULL, not `''` (#122).** The capture side
  sends an empty hash on purpose; the loader stored it verbatim, so `branch_hash IS NULL`
  undercounted sessions that cannot join a change request (ADR-005). `schema.sql` converts existing
  `''` rows, touching nothing else.
- **The compose `loader` service's traces load had never run (#121).** It called
  `python -m warehouse.load_traces` with no arguments while `--tempo` and `--dsn` were required, so
  every cycle ended in a usage error. That happened before a `loader_run` row could be written, and
  `|| echo "traces failed"` was the only sign. Both flags now fall back to `STDTEL_TEMPO` and
  `STDTEL_DSN`, which the service sets, and a test parses the service's own invocation.

## 0.7.0 — 2026-09-30

### Changed — breaking, on the wire and in the warehouse
- **Work joins change requests by branch identity, not a ticket parsed from the branch name
  (ADR-013).** The regex requirement was never communicated. Measured, it missed 2 of 9 of this
  repository's own branches and every branch of a repository that puts the issue number last, and
  it invented tickets: `RELEASE-0` on both releases, `ANALYZE-4` and `INIT-4` on Dependabot branches.
  - **On the wire:** spans carry `std.branch.hash`, a hash of the normalised repository and branch,
    never the branch name. `std.ticket.id` is gone. `std.repo` is now `owner/name`.
  - **Commit evidence:** the Stop hook records the `git patch-id` of each commit the session made, as
    `std.artefact.commit_patch_ids`. Patch-ids survive a rebase; the SHAs of the rebased PR #87 did not.
  - **The join:** `activation_change_request` (a view) assigns each activation to the change request
    its branch became, and records how: `branch`, `branch+commit` or `commit`. The scorecard's
    with-arm requires commit evidence, and a branch-only link is excluded from both arms.
  - **Delivery data:** it loads into a forge-neutral `change_request` record, with commits and ticket
    links, through a GitHub adapter. GitLab is #103. Closed-unmerged PRs are loaded.
  - **Tickets:** they are now enrichment, taken from the forge's issue links first and a fixed regex
    second. In a PR description, a key counts only after a linking word ("Fixes PLAT-9"). The first
    real run read ADR numbers cited in descriptions as tickets.
  - **Scope unit:** `ticket` is renamed `branch`.
  - **Warehouse:** `pull_request` and the `ticket_id` columns are dropped at cutover. Prior data is
    not migrated.

### Fixed
- **The demo fleet's skill invocations never shared a span id with their activations**, as the real
  loader's always do, so any join from one to the other found nothing.
- **`stdtel-query` gave a tool-failure query against `session_cost.tool_failures`**, a column that has
  never existed.

## 0.6.0 — 2026-09-28

### Added
- **External contract version 3 (ADR-012).**
  - `std.external.tool_use_id` joins an external run to the exact turn that made the MCP call. The
    Stop hook now records `mcp__*` tool-use ids from the transcript, including sub-agent transcripts,
    as `std.artefact.mcp_tool_use_ids`, and the loader writes them to `mcp_tool_call`. An MCP server's
    `session.id` goes stale after `/clear`, so the join is what attributes its spend correctly. Through
    the joined turn, external spend also gains a scope.
  - `std.external.requests_unpriced` keeps a partly priced run's observed cost as a lower bound
    instead of dropping it.
  - Query 7 version 3 reports partial runs apart from complete ones, and reports the join rate.
  - `stdtel-conform --print-contract` reports `contract_version: 3`. (#94)

### Fixed
- **`<synthetic>` is no longer counted as a model request.** Claude Code uses that name for
  placeholder assistant messages, such as interrupted or failed responses, which carry zero usage.
  Counting them inflated `llm_requests`, and when one came first in a turn it became the span's model:
  one Prometheus series was already labelled `gen_ai_request_model="<synthetic>"`. In 1,157 real
  turns, every turn that appeared to use more than one model was really one model plus `<synthetic>`.
- **A span names a model only when its run used exactly one.** This applies ADR-012 decision 5 to our
  own turns, sub-agents and skill tails. Genuinely multi-model runs were sub-agents, 5 of 200 sampled,
  and naming the first of two models said the run used one.
- **`scrub()` dropped every list attribute without a word.** It now passes a list of strings and
  nothing richer.
- **`stdtel-conform` read an OTLP `intValue` as a string**, and would have refused every correctly
  sent count.
- **Collector:** spanmetrics now counts across resources instead of per resource, so dashboards no
  longer read zero after a branch switch, a project change or an SDK upgrade. (#92)
- **`mise run load`** works again, having failed since #68, and the loader no longer reports keys that
  our own Langfuse overlay adds. (#93)

## 0.5.0 — 2026-09-28

### Changed
- **Activations are children of their turn, not flat roots (ADR-010).** A skill or sub-agent
  activation now carries the span id of the turn it ran under, so a trace shows the turn and what happened
  inside it. Compactions, and activations whose turn is not in the batch, stay roots, because that is
  what was observed. **Anyone querying Tempo for root spans will see fewer of them.** The warehouse
  gains `parent_span_id` and is otherwise unaffected. A session is deliberately never a parent: one real
  session spans seven weeks, which no trace store will hold. (#75, #80)
- **The ticket follows the branch, not the session.** The ticket key was read once, at session start,
  so a session that switched branches joined every later activation to the wrong PR. It is now
  refreshed on each event, and an empty read keeps the last known ticket rather than blanking it. (#77, #81)
- **A concurrent state write no longer ends a session's telemetry.** Sub-agent hooks fire while the
  parent is still calling tools, so two hook processes could write the state file at once and leave it
  unparseable. The hook swallowed the error and exited 0, and that session emitted nothing more. Writes
  are now atomic (a finished file is renamed into place). A torn file is salvaged from its intact
  leading document, which keeps the transcript offset, and the salvage is reported. Sub-agent and
  compaction hooks update the state file under a lock, so parallel writers no longer drop each
  other's updates. (#73, #74)

### Added
- **Containment across turns: scopes (ADR-010, ADR-011).** A skill that drives many turns (a loop
  over tickets) can declare `metadata.telemetry.scope: ticket`. Everything recorded while its container
  is open carries `std.scope.name` / `key` / `id`, and `warehouse/efficiency/06_scope_self_vs_inclusive.sql`
  reports its own cost beside everything incurred under it. **Self cost is stored; inclusive cost is
  derived** — no row holds a total including its children. A rollup answers containment, never
  causation, and the analyst brief says so. `stdtel-hook scope-close` ends a scope explicitly. Nothing
  is required of any author: an undeclared skill is turn-scoped. (#78, #79, #82)
- **Decorated sub-agents.** An agent file's `metadata:` block (`version`, `owner`, `standard_id`,
  `policy_ids`) is now captured on its activations. This works by tolerance, not contract: verified
  on Claude Code 2.1.277, undocumented upstream. Agents cannot declare a scope. MCP servers cannot be
  decorated at all and are overlay-only, which ADR-011 records as a non-goal. `stdtel-onboard` covers
  all three artefact types. (#83)
- **A fifth kind, `external`, for spend the harness cannot see (ADR-010 decision 7).** A process an
  agent calls, such as an MCP server that pays for its own model calls, reports its spend as
  `std.artefact.activation` spans with `kind=external`. `warehouse/efficiency/07_external_spend_and_coverage.sql`
  reports the spend and, separately, how much of it is known. (#86, #87)
- **`stdtel-conform`**, a new console script. It checks a foreign emitter's OTLP JSON against the
  external contract in *their* CI: span name, allowlist, no content, a named system, a well-formed
  session id, and costs that are numbers or absent. It reports cost coverage separately, because a
  shape-only check passes a file whose costs are missing. `--print-contract` prints the contract as JSON
  with a `contract_version`, for an emitter to diff its own copy against. (#87, #89)
- **External contract version 2: cost provenance.** `std.external.cost_usd` keeps meaning *observed*;
  `std.external.cost_source` (`provider` | `local`) says how; `std.external.cost_estimated_usd` carries
  the part priced from a list, beside it and never inside it. One run can mix billed and estimated
  calls, so one label over one figure had no honest value. Additive: every version-1 span stays valid.
  (#88, #89)
- **The loader names what it cannot load.** Tempo keeps an attribute nobody declared; the loader was
  where it vanished, without a word. It now names every unread key on an external span, with counts,
  and records them in `loader_run.unknown_attr_keys`. A renamed attribute therefore arrives as a named
  key rather than a column of NULLs. (#88, #89)
- **`skills/stdtel-instrument`**, a new skill. It adapts an external process to the contract and starts
  by reconciling the process's own records against the provider's bill. It records the verified fact
  that Claude Code strips an inherited `OTEL_*` variable before starting an MCP server, so the
  endpoint must be set in the server's `env` block. (#87, #89)
- **The primary metric has an input.** CI has uploaded a policy-results artefact on every PR run, and
  nothing ever collected it, so first-time OPA pass rate had no data while CI stayed green.
  `warehouse/fetch_policy_results.py` collects it. (#21, #72)

### Fixed
- **The loader window is guarded.** A sub-agent span is back-dated to the sub-agent's own start and is
  not searchable until Tempo's ingester flushes. A `--since` narrower than that delay stepped over
  such spans and never came back for them. It now warns. (#67, #71)
- **The release checks every console script.** The wheel check named seven scripts by hand and missed
  `stdtel-conform`, which would have shipped without ever running in the build. The list now has to
  cover `pyproject.toml`, and a test enforces it.
- **The exporter timeout is tested by behaviour.** `opentelemetry-exporter-otlp-proto-http` 1.45.0
  removed the private attribute the test read, so every CI run failed while the timeout still worked:
  1.00s against a collector that never answers. The test now measures exactly that. (#90)

## 0.4.0 — 2026-09-16

### Changed
- **The unit of capture is an artefact activation, not a skill invocation (ADR-009).** The span
  `std.skill.invocation` is renamed to `std.artefact.activation`, discriminated by
  `std.artefact.kind` ∈ `skill`, `subagent`, `compaction`, `turn`. **This is a wire-level rename** and
  anyone querying Tempo or Prometheus for the old name must update; the shipped dashboards and
  `deploy/smoke.sh` already have. There is deliberately no dual-emission period — two spans would have
  doubled every `traces_span_metrics_calls_total` series and both dashboards. Compatibility lives one
  layer down: `load_traces` still writes `skill_invocation` from kind `skill`, so `scorecard.sql`,
  every existing query and every row recorded before the rename keep working unchanged, and spans
  still in Tempo under the old name keep loading. (#63)

### Added
- **Sub-agent, compaction and turn capture.** A real session of this project's own spent $366, spawned
  42 sub-agents and compacted twice, and recorded one skill row and session totals — the numbers being
  collected could not answer "where did it go". New hooks `SubagentStart`, `SubagentStop` and
  `PostCompact`; where a hook has never fired the value is read from the session directory instead and
  flagged `std.artefact.source=transcript`, so an inference is never mistaken for an observation.
- **The session's real cost.** `session_cost.cost_usd` has been NULL for every row since the schema was
  written, because nothing read the harness's own `cost-state` entry. It and the harness's API and tool
  wall-clock totals are now loaded.
- **Per-hook latency on each turn**, as `std.turn.hook.<basename>.ms`. The basename only — the payload
  carries an absolute path, which is filesystem layout rather than data.
- **`warehouse/efficiency/`**: five versioned queries answering the named efficiency questions, and a
  session-efficiency brief in `agents/skill-scorecard-analyst` that runs them rather than writing its
  own. Efficiency is answerable for a single session at any volume; the primary effectiveness metric
  and its 30-merged-PRs-per-arm floor are unchanged.
- **Scheduled loaders.** `mise run load`, `mise run load-watch`, and an opt-in `loader` compose
  profile. Tempo held four days of spans the warehouse had never seen because `load_traces.py` is run
  by hand. Every run writes a `loader_run` row, including failures, and a dashboard panel shows how
  stale everything else on the page is.
- `stdtel-doctor` reports which of the new hook events have actually fired on this machine. Their
  payload shapes are transcribed from documentation, not captured, because hook configuration is read
  at session start and a session cannot observe a hook it just registered.

### Fixed
- **Dependabot branches minted phantom tickets** (`upload-artifact-7` → `ARTIFACT-7`), on spans as well
  as in the `ticket` table, putting work no developer did into the without-skill cohort. Both the
  capture and delivery parsers now anchor a ticket key to the start of a branch segment, bot-authored
  PRs are skipped, and a test pins the two parsers together because ADR-002 joins them on equality.
  (#62)
- **`span_id()` silently mangled hex ids.** A 16-character hex span id from Tempo's search API is also
  valid base64, so it was decoded into 12 bytes of a different id without raising. It now decodes only
  when the result is the length an id actually is.

### Known issue
- A sub-agent span stamped in the past is not searchable in Tempo until the ingester flushes its block,
  up to about half an hour later. Nothing is lost, but a loader window narrower than that delay would
  step over those rows permanently. (#67)

  > **Correction, 2026-09-16.** As shipped, this entry said such a span "never becomes searchable and
  > never reaches the warehouse". That was wrong: it was measured before the block had flushed and not
  > re-checked afterwards. The span appears, and the row loads. The real requirement is a loader window
  > wider than the flush delay, which the default already is; a guard and a test were added afterwards.

## 0.3.3 — 2026-09-14

### Changed
- **Every GitHub Action is pinned to a commit SHA**, with the version in a trailing comment. A tag is a
  pointer somebody else can move, and the job that publishes holds `id-token: write` — so whoever
  controlled `pypa/gh-action-pypi-publish@release/v1` controlled what shipped as `stdtel`,
  permanently. Two tests enforce it: no mutable reference anywhere, and every pin carrying the version
  it came from. (#34)
- **The published artefact is now verified after it lands.** A new `verify` job installs
  `stdtel==<tag>` from PyPI, runs its console scripts, checks the reported version, and verifies the
  PEP 740 attestation. Everything before it proved the wheel *we built* worked; nothing proved the one
  on the index did, and the two have differed before. It is not granted `id-token: write` — reading an
  attestation does not require minting a credential.
- **A release must have a matching section in `CHANGELOG.md`** or the build fails. Release notes are
  written from there, and a missing section means they are being improvised at the moment of shipping.
- **The sdist no longer ships `tests/`.** It carried `tests/*.py` without `conftest.py` or the
  fixtures, so the suite could not even be collected — worse than shipping none, because it reads as a
  broken package.

## 0.3.2 — 2026-09-14

### Added
- **Drift between the plugin and the package is now reported, in both directions.** They are separate
  installs and neither updates the other, so a developer updates one and assumes the other followed.
  Both failures are quiet: a missing package leaves the launcher exiting 0 in silence, and a stale
  plugin keeps working while the skills it ships lag the release.
  - `stdtel-doctor` gains a **plugin in step** check. Not having the plugin passes —
    `stdtel-install settings` is a supported install — but a version mismatch fails, naming the
    command that fixes it, which differs by direction.
  - `session_start` prints the same line once per session, on the only hook whose output a developer
    reads.
  - The plugin's launcher announces a **missing package** at session start. This closes a
    circularity: with the package absent nothing of ours runs, `stdtel-doctor` included, so the
    launcher is the only part of the install that can report it. Every other event stays silent — the
    same line on every tool call is noise, not information.

### Fixed
- **`stdtel.__version__` said `0.1.0`.** Hardcoded in the first commit and wrong for every release
  since; nothing read it, so nothing noticed. It is now derived from the installed package metadata,
  and the version-agreement test covers it — the skew check above depends on it being true.

## 0.3.1 — 2026-09-14

### Fixed
- **`stdtel-doctor` reported `0 attributed by overlay` while two skills were being attributed by one.**
  It counted manifests whose *file* sat in an overlay root, but `fill_gaps` returns a manifest based on
  the skill's own file, so a filled skill never matched. It now counts what the overlay actually
  contributed. Found by reading the number against a real overlay rather than trusting it.
- **Test isolation now clears every `STDTEL_*` variable** rather than a list of known ones. Once
  `STDTEL_SKILLS_OVERLAY` existed, a developer who had configured one saw two unrelated tests fail,
  because their real overlay leaked into assertions about an empty catalogue. Enumerating the
  environment cannot go stale; a list can.

## 0.3.0 — 2026-09-14

A minor rather than a patch: a new span attribute, a new environment variable and a new warehouse
column. Nothing is removed and no existing field changes meaning.

### Added
- **`std.skill.content_hash`** — SHA-256 of the SKILL.md body, truncated. Every version this system
  records is *asserted* by whoever wrote the front-matter; nothing ever checked it against the
  instruction the model was given, which ADR-005 forbids. The hash is observed: same version with two
  hashes is an edit that skipped the bump, and that scorecard row is aggregating two populations. The
  data-quality panel counts them. Deliberately not a spanmetrics dimension — a series per edit is #42
  again. (#52)
- **`STDTEL_SKILLS_OVERLAY`** — roots of front-matter-only stubs that attribute skills you do not own
  without editing them. Editing a third-party `SKILL.md` survives until the next upstream release,
  plugin reinstall or new machine, and fails silently each time. Overlays **fill gaps and never
  override**, the reverse of `STDTEL_SKILLS_ROOT`: an overlay that overrode would keep asserting its
  pinned version after upstream declared a real one, with nothing to notice it by. A stub may omit
  `version`, and never supplies a content hash — its body is the operator's note, not the skill's
  instruction. (#51)

### Changed
- **A manifest that fails the contract is kept in degraded form** rather than dropped by lenient
  loading, carrying its name and content hash. Dropping it cost us the only thing about an
  un-onboarded skill that can be observed rather than asserted.
- **Empty contract fields are omitted from spans** instead of emitted blank. A blank `std.standard_id`
  reads as a value in every dashboard that groups by it.
- `stdtel-doctor` reports unversioned and overlay-attributed counts.

### Fixed
- **`warehouse/schema.sql` now migrates an existing database.** `CREATE TABLE IF NOT EXISTS` does
  nothing to a table that already exists, so `content_hash` would have reached new warehouses only and
  the loader would have failed on its first INSERT after the upgrade.

## 0.2.5 — 2026-09-14

### Fixed
- **`stdtel-validate` said a manifest was invalid without saying which one**, and stopped at the first
  failure. Onboarding a directory of skills meant one run per problem, each run's output identical to
  the last. The path was available where the error was raised and was being thrown away. Every failure
  now names its file, all of them are reported in one run, and a manifest with no `metadata:` block at
  all is told that the contract fields live there — the thing "missing required field: version" does
  not convey to someone seeing it for the first time. (#49)

### Changed
- **`policy_ids` may be empty when the skill does not claim a policy signal.** It was required to be
  non-empty on every skill, which is a rule nobody can satisfy honestly: plenty of useful skills have
  no deterministic check, and the gate pushed people to cite an unrelated policy to get past it — worse
  than an empty list, because the scorecard would then score the skill against a rule it has nothing to
  do with. The rule now binds the pair: empty `policy_ids` is accepted only with
  `telemetry.success_signal: test` or `manual`, and the signal still defaults to `policy`.
  ADR-004 carries the reasoning; `skills/stdtel-onboard` is at 1.1.0 because its guidance changed.

## 0.2.4 — 2026-09-13

### Fixed
- **Span-metric counters could never increase, so three dashboard panels read zero at any volume.**
  Unset, the OpenTelemetry SDK generates `service.instance.id` per process, and
  `prometheusremotewrite` maps it to the `instance` label — so every hook, being its own process,
  opened a brand-new series which received exactly one increment and was abandoned. `rate()` needs
  two points in one series and always had one. Measured before the fix:
  `max(traces_span_metrics_calls_total)` = 1 across 563 samples, with cardinality growing by a series
  set per hook invocation. Identity is now derived rather than generated (`stdtel/identity.py`), and
  the metrics pipeline drops `service.instance.id` regardless of who sent it. `deploy/smoke.sh` emits
  from two separate processes and fails if the counter does not reach 2. (#42)
- **`distinct_users` read 0 for every skill, at every volume.** The collector derived `std.user.hash`
  from `user.email`, which nothing ever set, so the scorecard's `review-single-user` verdict could
  never fire and the "5+ developers" half of the reporting floor was measured by a column pinned at
  zero. The hook now emits a pseudonymous per-developer id — SHA-256 of uid and hostname, hashed
  before it leaves the process, never reversible to a name. (#43)
- **`stdtel-doctor` reported "hooks running" when the current project was recording nothing.** Every
  state file on the machine belonged to other projects; the session being watched had none. The check
  now says which project its newest data came from, and fails when none of it is from here. Claude
  Code reads hook configuration at session start, so a session already open when stdtel was installed
  runs no hooks for its entire life — `stdtel-install` now says so, and it is the first entry under
  "empty dashboards" in SUPPORT.md. (#44)

## 0.2.3 — 2026-09-12

First release published to PyPI: `uv tool install stdtel`.

### Changed
- **Publishing to the real index is release-only.** A manual dispatch skipped the version-vs-tag check
  (gated on `github.event_name == 'release'`), so any build could have gone out under any version —
  permanently, since PyPI never allows re-uploading a version. Manual dispatch now targets Test PyPI
  only. Added a pre-flight check that the version is free on the target index, so a collision names the
  version instead of surfacing as an opaque 400 at the final step.

### Fixed
- **Hooks failed with `stdtel-hook: command not found` on every tool call.** The shipped manifests used
  the bare command name, which does not resolve in a hook's non-login `sh -c`. They now invoke
  `${CLAUDE_PLUGIN_ROOT}/bin/stdtel-hook`, a launcher that finds the real CLI (`$STDTEL_HOOK_BIN`,
  `~/.local/bin`, the uv-tool and pipx locations, then PATH) and **exits 0 in silence when it finds
  none** — an uninstalled plugin should record nothing, not error on every keystroke. Amplified by
  0.2.0's unmatched `PostToolUse`, which fires on every tool rather than only Skill.

## 0.2.2 — 2026-09-12

### Fixed
- **Plugin failed to load its hooks.** `hooks/hooks.json` at the plugin root is discovered
  automatically, so `"hooks": "./hooks/hooks.json"` in the manifest registered it twice:
  *"Duplicate hooks file detected ... manifest.hooks should only reference additional hook files."*
  The declaration is removed; the file still ships and is still loaded. `claude plugin validate`
  accepts the duplicate, so this was only observable by installing.

## 0.2.1 — 2026-09-12

### Fixed
- **Plugin installation.** `agents` in the plugin manifest takes a list of agent *files*; 0.2.0 shipped
  `["./agents/"]`, a directory, and every install failed with `agents.0: Invalid input`. The manifests
  are now checked by `claude plugin validate` itself — 0.2.0's tests only compared them against our own
  `install.py::EVENTS` table, and one test asserted the broken value, pinning it.
- **`make down` left six containers running.** `docker compose down` ignores profiled services unless
  named, so the Langfuse containers survived and the network could not be removed
  (`Resource is still in use`). `down` now passes `--profile langfuse`.

### Changed
- A test that claimed to verify the scorecard agent's refusal behaviour was reading markdown for a
  substring; renamed to say what it checks. ADR-007 (proposed) records that **no test here verifies any
  behaviour of a skill or agent**, and issue #10 tracks adopting `claude plugin eval` for that.

## 0.2.0 — 2026-09-10

### Added
- `std.session.cost` spans: total session spend, emitted even when no skill loads, giving cost-per-PR
  a complete denominator. Loaded into `session_cost`.
- Tool-call counts and failures across every tool, carried on the session span.
- `warehouse/load_delivery.py`: GitHub → `ticket` / `pull_request` / `defect`, and `policy_result`
  from a CI artefact.
- `stdtel-install`, which binds hooks to an absolute path at install time.
- Plugin layout for three loaders: root `plugin.json` (Agent Plugins v1), `.claude-plugin/`,
  `com.github.copilot/`.
- `policies/`: `logging.required_fields`, `logging.no_pii`, `telemetry.manifest_valid` — the primary
  metric computes for real, where before `--dry-run` returned `True` for every policy.
- `skills/stdtel-onboard`, `skills/stdtel-query`, `agents/skill-scorecard-analyst`.
- `mise run smoke`: eight-hop verification ladder for the local stack.
- Docs: reference, skills, evaluation-power, insight-walkthroughs, local-stack, ADRs 002–005.

### Changed
- **`SKILL.md` contract fields moved under `metadata:`** for Agent Skills conformance. The flat form
  still parses, so existing catalogues keep validating.
- `stop()` returns the total number of spans emitted, which now includes the session-cost span.
- `PostToolUse` / `PostToolUseFailure` are no longer matched to `Skill`.
- Configuration moved to `STDTEL_OTLP_ENDPOINT` / `STDTEL_OTLP_TIMEOUT`; `OTEL_*` cannot reach a hook.
- `stdtel-validate` exits 2 on an unreadable root, where it previously exited 0.

### Fixed
- Empty `transcript_path` resolved to `"."` and killed all telemetry silently.
- The configured collector endpoint never reached the exporter, which fell back to localhost.
- A dead collector stalled the Stop hook ~7.3s per turn.
- Copilot events arriving via `~/.claude/settings.json` were stamped `claude-code`.
- Namespaced (`plugin:skill`) names matched no catalogue entry — 71% of real invocations.
- `std.skill.trigger` was a constant; it now comes from the transcript or reads `unknown`.
- `SessionState.save()` dropped fields it did not list, silently, across hook processes.
- Three never-executed bugs in `warehouse/load_traces.py` (OTLP id encoding, missing end time).

## 0.1.0

Initial scaffold: hooks, tail attribution, collector config, warehouse schema, eval runner.
