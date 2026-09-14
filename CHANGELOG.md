# Changelog

Versions are shared by the Python package and the plugin manifests, and a test asserts they agree.
**A version bump is what makes clients pick up a new copy** — both marketplaces serve the cached
version until this number changes — so bump it for anything a user would receive.

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
