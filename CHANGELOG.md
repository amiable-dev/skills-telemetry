# Changelog

Versions are shared by the Python package and the plugin manifests, and a test asserts they agree.
**A version bump is what makes clients pick up a new copy** — both marketplaces serve the cached
version until this number changes — so bump it for anything a user would receive.

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
