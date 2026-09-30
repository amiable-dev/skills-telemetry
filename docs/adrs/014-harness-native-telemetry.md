---
title: "ADR-014: Consume harness-native telemetry for cost; stdtel keeps the join, the standard and the outcome"
status: proposed
date: 2026-09-30
tags: [adr, telemetry, copilot, claude-code, cost, privacy, landscape]
links: ["001-distribution-and-capture-surface.md", "003-hook-execution-constraints.md", "005-data-integrity.md", "009-artefact-activation-as-the-unit-of-capture.md", "012-external-contract-v3.md", "013-join-work-to-change-requests.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/107"
---

## Context

stdtel was built when neither harness said what a skill cost. The hooks read Claude Code's
transcript and inferred it. A skill's "tail" is every request after it loads until the next skill
or the end of the turn. Its `load_tokens` is characters divided by four. Copilot was left to its
native OpenTelemetry, mapped onto `std.skill.*` by a hand-maintained table.
[ADR-001](001-distribution-and-capture-surface.md) left one question open ever since: consume
Claude Code's native telemetry instead. CLAUDE.md lists two Copilot attributes as unverified,
`skill_name` and `github.copilot.git.*`.

Both harnesses have since moved. The full survey, with sources, is in
[docs/landscape.md](../landscape.md). What bears on this decision, all verified in the vendors'
documentation on 2026-09-30:

**Claude Code measures per-skill cost itself**
([monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)).
- Every model request emits a `claude_code.api_request` event, and increments
  `claude_code.cost.usage` and `claude_code.token.usage`. Each carries `skill.name`, `agent.name`,
  `plugin.name`, `mcp_server.name`, `mcp_tool.name`, `query_source` and `model`. The event also
  carries `cost_usd`, the token counts, `prompt.id` and `session.id`.
- That is exact attribution per request. stdtel's tail rule approximates it, and its `load_tokens`
  once recorded 7 against a tail of 28,199.
- Two conditions come with it:
  - Skills from **third-party** plugins, which includes this project's own marketplace, are named
    `"third-party"` unless `OTEL_LOG_TOOL_DETAILS=1`. That flag also exports tool inputs and full
    commands.
  - `user.email` and account identifiers are standard attributes, **on metrics as well as
    events**.

**Copilot emits native OpenTelemetry in every surface, but not per-skill cost**
([VS Code](https://code.visualstudio.com/docs/agents/guides/monitoring-agents), doc dated
2026-09-16; [vscode-copilot-chat](https://github.com/microsoft/vscode-copilot-chat/blob/main/docs/monitoring/agent_monitoring.md);
[GitHub Docs](https://docs.github.com/en/copilot/concepts/enterprise/opentelemetry)).
- **Surfaces.** VS Code, the CLI, JetBrains and the Copilot app export GenAI-convention traces,
  metrics and events. Enterprises can mandate the collector.
- **Skills.** `execute_tool` spans carry `github.copilot.tool.parameters.skill_name` when a skill
  runs.
- **Git context.** Spans carry `github.copilot.git.repository`, `.branch` and `.commit_sha`.
- **Tokens and cost.** Token usage has **no skill or agent dimension**, and **no cost is emitted**
  (Copilot bills in AI credits).
- **Content.** The docs list `github.copilot.tool.parameters.command` and `.file_path` as
  content-bearing.

**Nobody else does what this project is for.**
- Anthropic's [contribution metrics](https://code.claude.com/docs/en/analytics) and GitHub's
  [Copilot usage metrics](https://github.blog/changelog/2026-09-25-usage-metrics-api-adds-pull-request-review-stages/)
  measure one tool's PRs, with versus without.
- Engineering-intelligence platforms ([Faros](https://www.faros.ai/blog/claude-code-analytics),
  [Jellyfish](https://jellyfish.co/library/tools-to-measure-ai-developer-productivity/)) "stop at
  the boundary of the tool".
- Skill benchmarks ([SkillsBench](https://www.alphaxiv.org/abs/2602.12670),
  [Tessl](https://tessl.io/registry)) are offline.

No one measures whether a *specific skill* changes outcomes on a team's own change requests.

**Verified live, 2026-09-30, Claude Code 2.1.284.** One headless session was exported to a scratch
collector with every content flag off, and the export was then deleted because it held real identity
values:
- **The bridge holds.** The `prompt.id` on the native `api_request` event equals the `promptId` in
  that session's transcript. Decision 7 rests on this, and until this run it was assumed, not
  measured.
- **Identity is on every record, not on the resource.** `user.email`, `user.account_uuid`,
  `user.account_id`, `user.id` and `organization.id` are attributes of every metric data point and
  every event. The resource carries only host and service.
- **`session.id` is a label on every native metric.**
- **`prompt` and `response` keys are present** on `user_prompt` and `assistant_response`. Their value
  is the literal `<REDACTED>` when the content flags are off.

**Three things in this stack would break the moment native telemetry was switched on.**
- The collector pseudonymises only the traces pipeline, so `user.email` would reach Prometheus.
- The logs pipeline exports only to `debug`, so every per-request event would be discarded.
- The content rules miss both harnesses' native content keys.

## Options considered

- **Keep deriving cost for both harnesses.** Rejected. It estimates what Claude Code now measures,
  and ADR-005's first rule is not to record an inference where an observation exists.
- **Replace stdtel with native telemetry and an off-the-shelf dashboard.** The community stacks
  ([claude-code-otel](https://github.com/ColeMurray/claude-code-otel),
  [SigNoz](https://signoz.io/docs/claude-code-monitoring/)) show cost and usage. Rejected: they stop
  at the tool, and none joins to change requests, knows a skill's version or standard, sees spend
  inside MCP servers, or refuses to compare below a sample-size floor.
- **Use Anthropic's contribution metrics as the join.** Rejected as the join; adopted as evidence.
  It matches content, which ADR-013 does not, but it is Claude-only, needs a Team or Enterprise
  plan and the GitHub app, is unavailable to API customers, and works per tool. Its
  `claude-code-assisted` label is useful arm evidence (decision 9).
- **Adopt Agent Trace or Git AI for provenance.** Deferred.
  [Agent Trace](https://agent-trace.dev/) is at v0.1.0 (RFC), and neither harness emits it natively.
  If both do, line-level provenance could replace ADR-013's patch-id evidence.
- **Claude Code's trace beta** (`claude_code.interaction` spans, `workflow.run_id`, `TRACEPARENT`
  into subprocesses). Deferred until it is out of beta. Verify then whether `TRACEPARENT` reaches
  MCP servers. If it does, it may be a cleaner answer to ADR-012's `/clear` problem than
  `toolUseId`.
- **Send native metrics to Prometheus.** Rejected. Every native Claude Code metric carries
  `session.id` as a label, so it opens a series per session. Deleting the label to avoid that
  recreates #92: concurrent sessions writing different cumulative values to one series. The
  per-request events carry everything the metrics do, and more, so the metrics add nothing the
  warehouse needs. Copilot's are no different.
- **Store events with the collector's `file` exporter, tailed by the loader.** Rejected in favour of
  Loki. It is lighter, but the loader would own rotation, partial lines and offsets: the problems
  the transcript reader already has, repeated. Loki gives indexed reads by time range, which the
  loader needs, and Grafana reads it directly.
- **Turn on `OTEL_LOG_TOOL_DETAILS=1` silently, by default, so third-party skills are named.**
  Rejected. The flag is the first time this project would have a harness emit content:
  `tool_input` for `Write` and `Edit` is file contents, and it travels as far as the collector
  before being deleted. A setting like that is the user's decision, made where they can see what it
  costs, not a default they never saw. Decision 12 makes it a recommended, consented opt-in.
- **Leave it off everywhere, and name skills another way.** Kept as the fallback for anyone who
  declines, but not recommended: it loses the detailed view, and it cannot name a third-party skill
  that shares a prompt with another. The two other ways to name skills are:
  - install this project's skills as user-defined, by linking them into `~/.claude/skills` as the
    README already recommends; user-defined skills are named verbatim;
  - resolve `"third-party"` from stdtel's own skill activation on the same `prompt.id`, when exactly
    one third-party skill ran in that prompt. That row is labelled `derived`, as Copilot's are.
- **Consume native telemetry for what the harnesses measure, and keep stdtel for what nothing
  measures, for both harnesses** — chosen.

## Decision

1. **A harness's own measurement is the source of per-request tokens and cost, wherever one
   exists.** stdtel stops estimating what a harness observes.
   - **Claude Code:** tokens, cost and the skill, agent, plugin and MCP attribution come from
     `claude_code.api_request` events.
   - **Copilot:** tokens come from its `chat` spans. Cost does not exist, so Copilot is compared
     in tokens, never in currency (unchanged).

2. **Both harnesses are configured by `stdtel-install`, and neither exports content.**
   - **Claude Code** settings `env`:
     - `CLAUDE_CODE_ENABLE_TELEMETRY=1`
     - `OTEL_LOGS_EXPORTER=otlp`. `OTEL_METRICS_EXPORTER` is left unset, so no native metrics are
       sent (decision 4).
     - `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, with the collector endpoint
     - `OTEL_METRICS_INCLUDE_REPOSITORY=true`, which applies to events too
     - `OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false`: identity withheld at the source, not only deleted
       downstream
     - `OTEL_LOG_TOOL_DETAILS=1` is **recommended, and enabled only through decision 12**: after the
       user consents and the collector has proven it drops content. The non-interactive
       `stdtel-install` never sets it. For anyone who declines, third-party skills are named from
       stdtel's own activation on the same `prompt.id` (decision 10).

     Prompts, responses and raw API bodies stay off. These are Claude Code's own variables, not a
     hook's, so ADR-003's `OTEL_*` scrubbing does not apply.
   - **Copilot:**
     - VS Code: `github.copilot.chat.otel.enabled: true`, with
       `github.copilot.chat.otel.otlpEndpoint`, and `captureContent: false`, written or printed by
       a new `stdtel-install copilot`;
     - CLI: `COPILOT_OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT`;
     - organisations: the enterprise-managed `telemetry` block, documented as the preferred route.

3. **The collector enforces privacy on every pipeline, for both harnesses.** Metadata-only has
   always been enforced where the data lands (ADR-005). It now covers traces, metrics **and** logs:
   - **Content deleted:**
     - Claude Code: `tool_input`, `tool_parameters`, `full_command`, `bash_command`, `error`,
       `prompt`, `response`;
     - Copilot: `github.copilot.tool.parameters.command` and `.file_path`, plus the `gen_ai.*`
       message and argument keys already covered.
   - **Identity, at record level.** Verified live, these are attributes on every data point and
     event, so a resource-level rule never sees them. On logs and spans, `user.email` is replaced
     by `std.user.hash`, and `user.account_uuid`, `user.account_id`, `user.id` and
     `organization.id` are deleted. The `<REDACTED>` `prompt` and `response` keys are deleted too:
     a placeholder today is a value if a flag is flipped.
   - **Copilot's branch** is hashed into `std.branch.hash` with ADR-013's algorithm, then the plain
     name is deleted, so Copilot matches Claude Code's "hash only past the collector". A test holds
     the collector's hash equal to `stdtel.enrich.branch_hash`, running the pinned collector image,
     as #92 did. **If OTTL cannot reproduce `repo_id`'s normalisation exactly**, the fallback is to
     hash in the loader and keep plain branch names in Tempo. That is a privacy regression, which
     must be decided explicitly, not discovered in production.

4. **Events get a store, and native metrics do not go to Prometheus.** Loki joins the compose
   stack, and the logs pipeline exports to it after decision 3. Claude Code's per-request records
   exist only as events, because the metrics are aggregate counters with no `prompt.id`, so dropping
   events drops the attribution. Native metrics are not exported: Claude Code's are off at the
   source (decision 2), and Copilot's are filtered at the collector. The warehouse, built from
   events and traces, is the one analytic path for native records. Prometheus keeps stdtel's
   spanmetrics, whose cardinality this project controls (#42, #92).

5. **One table of model requests, for both harnesses.** `llm_request` holds:
   - identity: harness, `session_id`, `prompt_id` (Claude Code) or conversation and trace ids
     (Copilot), `request_id`, `started_at`, `model`;
   - usage: tokens by type, and `cost_usd` (NULL for Copilot — never zero);
   - attribution: `skill_name`, `agent_name`, `plugin_name`, `mcp_server`, `mcp_tool`,
     `query_source`, `branch_hash`;
   - `attribution_source`: `native` or `derived`.

   Claude Code rows come from Loki, Copilot rows from Tempo. Per-skill cost is a sum over this
   table, and every panel says which `attribution_source` it read. **`llm_request` is authoritative
   per request.** `std.session.cost` stays as the harness's own cumulative total, used only to
   reconcile against the sum of requests, and the two are never added together.

6. **Copilot's per-skill attribution is derived, and says so.** Within one `invoke_agent` trace, the
   `chat` spans after a skill's `execute_tool` span belong to that skill, until the next skill or
   the end of the agent turn. That is the tail rule, kept only where the harness gives nothing
   better, and every row it produces is `attribution_source = derived`. Claude Code rows are
   `native`. A comparison mixing the two says so, because the Copilot figure is an attribution
   while the Claude Code figure is a measurement. This rests on an **assumption the Copilot fixture
   must confirm before the loader is trusted**: that a skill's `execute_tool` span precedes the
   `chat` spans that used it, within one `invoke_agent`.

7. **Native records join change requests through ADR-013, for both harnesses.**
   - **Claude Code:** `llm_request.prompt_id` joins the stdtel turn carrying the same `prompt.id`.
     That gives its branch hash, and `activation_change_request` does the rest. The bridge is the
     reason stdtel's hooks keep recording `prompt.id`, and it was verified live (see Context).
   - **Copilot:** requests carry `std.branch.hash` from decision 3, so they join by branch and
     time directly.
   - A second view, `llm_request_change_request`, applies ADR-013's rule to requests.

8. **Commit evidence for Copilot comes from HEAD transitions.** Spans carry the current commit.
   A SHA that first appears during a session, and later appears in a change request's commit
   list, is evidence for it. The forge adapter already turns any SHA into a patch-id.
   `commit_evidence` gains a `source` column: `hook_patch` (Claude Code, authored by the local
   identity) or `head_transition` (Copilot: observed, authorship not checked). The weaker source is
   visible wherever it is used.

9. **Forge signals of agent authorship are evidence for the arm.** The GitHub adapter reads
   Anthropic's `claude-code-assisted` label as Claude Code involvement, beside the explicit arm
   labels. The #62 bot filter must stop discarding **coding-agent** PRs. It was written to exclude
   dependency bots, and it very likely drops every PR opened by the Copilot coding agent or the
   Claude GitHub app too, because it matches any `[bot]` or `app/` author. Unverified: no such PR
   has been loaded yet. Agent logins are recognised as agent work for that harness, and dependency
   bots stay excluded. Verify each login against a real PR before relying on it.

10. **stdtel's Claude Code hooks narrow to what no harness records.** They keep:
    - the branch hash and commit patch-ids (ADR-013);
    - `prompt.id`, the bridge in decision 7;
    - catalogue identity: version, `standard_id`, `policy_ids`, content hash;
    - scope and containment (ADR-010), and sub-agent manifests (ADR-011);
    - the external contract (ADR-012);
    - the structural record of activations.

    With `OTEL_LOG_TOOL_DETAILS` off, a native request whose `skill.name` is `"third-party"` is
    named from stdtel's own skill activation on the same `prompt.id`, when exactly one third-party
    skill ran in that prompt. It is labelled `attribution_source = derived`. When more than one ran,
    it stays `"third-party"` rather than being guessed.

    They stop attributing tokens. The tail rule, `tail_tokens_first_only` and chars/4
    `load_tokens` are removed one release after native records are verified live. Until then the
    transcript path runs only when no native record exists for the prompt, labelled
    `source=transcript`.

11. **The Copilot skill map is retired** once `github.copilot.tool.parameters.skill_name` is seen
    in a live trace **with `captureContent: false`**. It sits among `tool.parameters.*` siblings
    that are content-only, so if it is only emitted with content capture on, decisions 2 and 11
    conflict, and the map stays. That closes CLAUDE.md's next step 3. `collector/copilot-skill-map.yaml`
    stays until then.

12. **A setup skill configures both harnesses, and is where the detailed view is offered.**
    `skills/stdtel-setup` is interactive setup for an agent to run with the user. It:
    - configures **Claude Code** with the decision 2 variables, through `stdtel-install settings`;
    - configures **Copilot** in whichever surfaces the user has: VS Code settings through
      `stdtel-install copilot`, the CLI's environment, or, in an organisation, the
      enterprise-managed `telemetry` block, which it explains rather than edits;
    - **recommends `OTEL_LOG_TOOL_DETAILS=1`, and explains in plain terms what it costs:** tool
      inputs, including file contents from Write and Edit, travel to the collector and are deleted
      there;
    - **checks before it enables the flag.** The collector must be local, or one the user confirms
      applies the same rules, and the content check in decision 13 must pass. If either fails, it
      does not enable the flag and says why;
    - enables the flag **only on the user's explicit yes**, then runs `stdtel-doctor` and reports.

    The setup skill decides nothing on the user's behalf. It makes the choice informed, and it makes
    the choice checkable.

13. **Content-dropping is tested on each machine, not assumed.** `stdtel-doctor` gains a
    `content dropped` check, run whenever the flag is on, and by decision 12 before it is turned on.
    - It sends one probe span and one probe event to the configured collector, each carrying a
      content-shaped attribute whose value is a random marker, under `service.name =
      stdtel-probe`.
    - After the flush interval, it confirms the marker appears nowhere it can query: Tempo and Loki.
    - The check fails loudly if the marker is found, and says the flag must be turned off (ADR-005:
      a component that cannot do its job fails loudly).

    The loaders ignore `service.name = stdtel-probe`, so a probe never becomes a row.

14. **The project rule on content is reworded, because it is being narrowed deliberately.**
    CLAUDE.md now says "never emit prompt/response/file content". With decision 12's opt-in, the
    harness does emit content — as far as a collector that deletes it. On acceptance, the rule
    becomes:

    > **Nothing stored or forwarded past the collector ever holds content** (prompts, responses, file
    > contents, command lines, tool inputs or results). Content reaches the collector only by the
    > user's explicit, informed opt-in, made through `stdtel-setup`, and only once the collector has
    > proven, on that machine, that it drops it.

    stdtel's own hooks and exporter still never emit content at all. The narrowing covers only what
    a harness sends to the collector when the user has chosen it.

## Consequences

- Per-skill cost for Claude Code becomes a measurement, and per-skill tokens for Copilot become a
  labelled attribution. The gap between the two harnesses is visible, not hidden.
- The primary metric is unchanged: first-time pass rate, with versus without, at change-request
  grain, above the floor. It gains exact cost beside it.
- The code shrinks where others now do the work. The tail rule, `load_tokens` and the Copilot skill
  map go.
- ADR-001's open question is closed, and CLAUDE.md's next steps 1 and 3 are resolved by this ADR.
- The stack gains Loki. The collector gains privacy rules on two more pipelines, which are needed
  whatever else is decided, because switching native telemetry on without them would put email
  addresses into Prometheus.

### Known limitations

- **If the user opts into `OTEL_LOG_TOOL_DETAILS=1`, content travels as far as the collector.** It
  is deleted there. Decision 13 proves the deletion on the machine, but only for the stores it can
  query. A shared or remote collector that forwards somewhere else must apply the same rules, and
  the check cannot see past it. That is why decision 12 asks the user to confirm a non-local
  collector, rather than trusting it.
- **Content crosses the network hop to the collector before it is deleted.** For a local collector
  that hop is loopback. Anywhere else, the transport and the collector's own logging are in scope.
- **With the flag off, a third-party skill that shares a prompt with another cannot be named.**
- **Copilot per-skill tokens are derived, not measured.** Copilot has no cost at all.
- **The Copilot attributes are verified in documentation, not in a live trace.** The hooks were
  validated against captured payloads (2026-09-10), and each harness's native telemetry needs the
  same before its loader is trusted: a recorded fixture, replayed by tests.
- **Coverage of JetBrains and the Copilot app is documented, but attribute parity is not.** Treat
  each surface as unverified until a trace from it is captured.
- **Copilot commit evidence is weaker.** A HEAD transition does not prove the agent wrote the
  commit.
- **Anthropic's `claude-code-assisted` label exists only on Team and Enterprise plans** with the
  GitHub app. Its absence is unknown, never "not assisted".
- **Vendor telemetry changes monthly.** The attributes named here are pinned to the documentation
  dates in [docs/landscape.md](../landscape.md) and should be re-read before each release.

## Related

- [ADR-001](001-distribution-and-capture-surface.md): its open question is closed here.
- [ADR-003](003-hook-execution-constraints.md): its `OTEL_*` scrubbing applies to hooks, not to the
  harness's own exporter.
- [ADR-005](005-data-integrity.md): observation over inference, and missing data as its own
  category (NULL cost for Copilot).
- [ADR-009](009-artefact-activation-as-the-unit-of-capture.md): activations stay as the structural
  record; their token fields are superseded.
- [ADR-012](012-external-contract-v3.md): external spend is still invisible to both harnesses;
  revisit its join if `TRACEPARENT` reaches MCP servers.
- [ADR-013](013-join-work-to-change-requests.md): the join both harnesses' records flow through.
- [docs/landscape.md](../landscape.md): the survey behind this ADR, with every source.
