# The landscape: what else measures agent work, and what it does not

Researched 2026-09-30 for [ADR-014](adrs/014-harness-native-telemetry.md). Where a claim below says
**verified**, it was read in the vendor's own documentation on that date. Where it says **reported**,
it comes from a third-party source only. Nothing here was verified against a live trace unless it
says so. Primary documentation changes monthly: re-read it before acting on any detail.

## What this project does, in one line

It asks whether a **standard, packaged as a skill, changes outcomes on real change requests**. The
measure is first-time policy pass rate, with the skill versus without, at PR grain, above a
sample-size floor. It asks this for Claude Code and GitHub Copilot alike, and records what each
skill, sub-agent and external process cost along the way.

Every tool below answers part of that question. None answers all of it.

## 1. Harness-native telemetry

### Claude Code — verified ([monitoring-usage](https://code.claude.com/docs/en/monitoring-usage))

- **Per-request attribution.** `claude_code.cost.usage` (USD) and `claude_code.token.usage`
  (`type` = input / output / cacheRead / cacheCreation) carry `skill.name`, `agent.name`,
  `plugin.name`, `marketplace.name`, `mcp_server.name`, `mcp_tool.name`, `query_source`
  (main / subagent / auxiliary) and `model`. So do the `claude_code.api_request` events, which also
  carry `cost_usd`, the token counts, `request_id` and `duration_ms`.
- **Skills.** `claude_code.skill_activated` carries `skill.name`, `invocation_trigger`
  (`user-slash`, `claude-proactive`, `nested-skill`), `skill.source` and `skill.kind`.
- **Correlation.** `prompt.id` links every event from one prompt; `session.id` is on everything;
  `tool_use_id` links tool events.
- **Repository.** With `OTEL_METRICS_INCLUDE_REPOSITORY=true` (v2.1.269+), everything carries
  `vcs.repository.url.full`, `vcs.owner.name`, `vcs.repository.name` and `vcs.provider.name`
  (github, gitlab, bitbucket, gitea).
- **Commits.** A successful `git commit` produces a `tool_result` event with
  `vcs.ref.head.revision` (the SHA) and `vcs.ref.head.name` (the branch). This only happens with
  `OTEL_LOG_TOOL_DETAILS=1`, which also exports `tool_input` and full commands — content.
- **Redaction.** Skill, plugin and MCP names from **third-party** plugins, which includes this
  project's own `amiable-standards` marketplace, are replaced by `"third-party"` unless
  `OTEL_LOG_TOOL_DETAILS=1` is set.
- **Personal data.** The standard attributes include `user.email`, `user.account_uuid`,
  `user.account_id` and `organization.id`, on metrics as well as events.
- **Traces (beta).** Set `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` to get:
  - spans: `claude_code.interaction` per prompt, `claude_code.llm_request` and `claude_code.tool`;
  - attributes on them: `agent_id`, `parent_agent_id`, `workflow.run_id`, `workflow.name`;
  - propagation: active tool spans set `TRACEPARENT` for subprocesses.
- **Also:** `claude_code.pull_request.count` and `claude_code.commit.count` metrics.

**Verified live, 2026-09-30, Claude Code 2.1.284**. One headless session was exported to a scratch
collector with content flags off, and the export was deleted afterwards because it held identity
values.
- The `prompt.id` on `api_request` equals the transcript's `promptId`.
- `user.email`, `user.account_uuid`, `user.account_id`, `user.id` and `organization.id` are
  attributes on **every metric data point and event**. The resource carries only host and service.
- `session.id` labels every metric.
- `prompt` and `response` are present, with the value `<REDACTED>`.
- The events seen: `api_request`, `user_prompt`, `assistant_response`, `hook_registered`,
  `hook_execution_start`, `hook_execution_complete`, `plugin_loaded`, `mcp_server_connection` and
  `managed_settings_resolved`.

### GitHub Copilot — verified ([VS Code: monitor agent usage](https://code.visualstudio.com/docs/agents/guides/monitoring-agents), doc dated 2026-09-16; [vscode-copilot-chat monitoring](https://github.com/microsoft/vscode-copilot-chat/blob/main/docs/monitoring/agent_monitoring.md); [GitHub Docs](https://docs.github.com/en/copilot/concepts/enterprise/opentelemetry))

- **Coverage.** Copilot Chat in VS Code and the Copilot CLI ship a native OTLP exporter.
  [JetBrains](https://github.blog/changelog/2026-07-27-github-copilot-for-jetbrains-adds-improvved-opentelemetry-configuration-and-model-management/)
  and the [Copilot app](https://github.blog/changelog/2026-09-22-opentelemetry-in-the-github-copilot-app/)
  gained it in July and September 2026. Since July 2026, an enterprise can
  [mandate the collector](https://github.blog/changelog/2026-07-08-enterprise-managed-opentelemetry-export-for-vs-code-and-cli/).
- **Spans** follow the GenAI conventions: `invoke_agent`, `chat`, `execute_tool` and
  `execute_hook`, with `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.request.model`,
  `gen_ai.usage.*` and `gen_ai.tool.name`.
- **Skills.** `github.copilot.tool.parameters.skill_name` is recorded when a skill is invoked.
- **Git context.** `github.copilot.git.repository`, `github.copilot.git.branch`,
  `github.copilot.git.commit_sha` and `github.copilot.github.org` (GitHub remotes only).
- **Token usage has no skill or agent dimension**, and **no cost is emitted anywhere**. Copilot
  bills in AI credits.
- **Outcome-shaped metrics:** `copilot_chat.pull_request.count`, `copilot_chat.edit.survival.*`
  and `copilot_chat.lines_of_code.count`.
- **Content.** Capture is off by default. The docs list `github.copilot.tool.parameters.command`,
  `.file_path` and `.mcp_server_name`, among others, as content-bearing. microsoft/vscode#326254
  reports content in spans despite `captureContent:false`, so enforcement belongs in the collector.

## 2. Vendor analytics that join usage to delivery

- **Anthropic contribution metrics** — verified ([analytics](https://code.claude.com/docs/en/analytics),
  [announcement](https://claude.com/blog/contribution-metrics)):
  - What they measure: merged PRs and lines, with versus without Claude Code.
  - How: by matching the content of session edits against each PR's added lines, from 21 days
    before merge to 2 days after. The branch is ignored.
  - Output: matched PRs get the GitHub label `claude-code-assisted`.
  - Limits: Team and Enterprise plans only, with the GitHub app; not available for API customers.
  - What's absent: nothing per skill or plugin, and no quality or outcome measure.
  - Also: the [Claude Code Analytics API](https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api)
    gives daily per-user usage.
- **GitHub Copilot usage metrics API** — verified from the changelog:
  - [per-repository](https://github.blog/changelog/2026-07-17-repository-level-github-copilot-usage-metrics-generally-available/)
    activity from the coding agent and code review;
  - [PR throughput and time to merge](https://github.blog/changelog/2026-02-19-pull-request-throughput-and-time-to-merge-available-in-copilot-usage-metrics-api/);
  - [review stages](https://github.blog/changelog/2026-09-25-usage-metrics-api-adds-pull-request-review-stages/).

  All per tool, never per skill.
- **Engineering-intelligence platforms** — reported: [Jellyfish](https://jellyfish.co/library/tools-to-measure-ai-developer-productivity/),
  [Faros AI](https://www.faros.ai/blog/claude-code-analytics), and
  [DX, LinearB and Swarmia](https://blog.exceeds.ai/dx-linearb-swarmia-productivity-benchmark/).
  - What they do: join AI-tool usage and spend to throughput, cycle time, incidents and DORA.
  - Faros's own summary of vendor analytics: they "stop at the boundary of the tool".
  - What they don't do: attribute anything below the tool, or publish how they match sessions
    to PRs.

## 3. AI code attribution standards

- **[Agent Trace](https://agent-trace.dev/)** is Cursor's open specification (v0.1.0, January 2026,
  RFC status). It records, for ranges of code, whether each came from a human, an AI, both, or is
  unknown, with links to the conversation behind it. It can be stored in files, git notes or a
  database. It's implemented by Cline and OpenCode and adopted by Git AI.
  [Thoughtworks Radar](https://www.thoughtworks.com/en-us/radar/platforms/agent-trace) lists it.
- **[Git AI](https://github.com/git-ai-project/git-ai)** attributes code to AI line by line, stored
  in git notes.

Both answer *which code came from an agent*. They do not answer *which skill, and did it help*.

## 4. Evaluating skills

- **Benchmarks** run each task with and without a skill. Examples:
  [SkillsBench](https://www.alphaxiv.org/abs/2602.12670), where curated skills raised the average
  pass rate from 33.9% to 50.5% across 18 configurations and self-generated skills were sometimes
  harmful; SWE-Skills-Bench; and SkillAudit ([survey](https://arxiv.org/html/2606.11435v1)).
- **The [Tessl registry](https://tessl.io/registry)** scores skills on
  [review, activation and impact](https://docs.tessl.io/improving-your-skills/evaluating-skills),
  with security scores via Snyk.

All offline and scenario-based. None measures a skill's effect on a team's own change requests over
time.

## 5. Community stacks and LLM observability

- **Community stacks.** Several open-source stacks chart Claude Code's native metrics in Grafana:
  [claude-code-otel](https://github.com/ColeMurray/claude-code-otel),
  [claude-code-metrics-stack](https://github.com/acreeger/claude-code-metrics-stack),
  [dashboard 25052](https://grafana.com/grafana/dashboards/25052-claude-code/).
  [SigNoz](https://signoz.io/docs/claude-code-monitoring/) documents both harnesses. All stop at
  usage and cost.
- **LLM observability tools.** Langfuse, Arize Phoenix and Braintrust trace LLM applications and
  support evaluations, and Langfuse has
  [guidance on Claude Code](https://langfuse.com/resources/engineering). Their unit is the trace,
  not the change request ([ADR-006](adrs/006-langfuse-as-an-optional-trace-backend.md)).

## What this leaves stdtel to do

| capability | who already does it | stdtel's role |
|---|---|---|
| per-skill / sub-agent / MCP cost, Claude Code | Claude Code native (exact, per request) | **consume it** |
| per-skill tokens, Copilot | nobody; Copilot has no skill dimension on tokens | derive it from Copilot's span tree |
| cost, Copilot | nobody; AI credits, not emitted | compare tokens, never currency |
| join work to change requests without a naming convention | Anthropic (content matching, Team/Enterprise, per tool) | ADR-013's branch + commit join, for both harnesses |
| skill identity: version, standard, policies, content hash | nobody | **keep** |
| per-skill outcome, with vs without, at PR grain, above a floor | nobody (benchmarks are offline) | **keep: the reason this project exists** |
| spend inside MCP servers and tools the harness cannot see | nobody | **keep** (ADR-010 decision 7, ADR-012) |
| containment of loop skills | partly: Claude Code beta `workflow.run_id` | **keep**; revisit when the beta settles |
| metadata-only, enforced where the data lands | nobody; both harnesses leak content in some configurations | **keep**, and widen to both harnesses' native keys |
