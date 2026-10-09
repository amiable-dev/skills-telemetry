---
title: "ADR-015: Telemetry health is a mechanism, not an instruction"
status: accepted
date: 2026-10-09
tags: [adr, health, doctor, loader, statusline, agents]
links: ["003-hook-execution-constraints.md", "005-data-integrity.md", "009-artefact-activation-as-the-unit-of-capture.md", "014-harness-native-telemetry.md"]
---

## Context

On 2026-10-09 five Claude Code sessions had been active for 24 hours. All five were "set up for
skills-telemetry". Their transcripts were compared with what each store held:

| session | stdtel spans (Tempo) | Claude Code's own records (Loki) |
|---|---|---|
| sightline, learnlock-studio, breach-resolver | yes | yes |
| docusaurus | yes | **none**: 295 model requests missing from every spend panel |
| skills-telemetry | yes | **none** |

The docusaurus session started on 2026-09-29. The native-telemetry settings were written on
2026-10-01 (ADR-014 decision 2). A session reads its settings when it starts, and resuming it is not
starting it, so that session will never export. At least seven more running sessions predate
2026-10-01. Nothing told anyone. The settings were correct, the collector was healthy, and the data
simply never existed.

Separately, the warehouse had not been loaded since 2026-10-02, when it was loaded by hand.
`loader_run` recorded that fact faithfully (ADR-009 decision 7), and nothing read it. Every
warehouse panel was a week stale while looking current. The first attempt to catch up then failed
outright: `load_traces --since 192h` asks Tempo for a window wider than it allows, and the loader
neither caps nor splits the window.

These are the same failure twice. ADR-005 says a component that cannot do its job fails loudly. Both
failures were loud only to someone who already knew where to look.

The maintainer asked whether the answer is "add detail to the agent's skills and instructions so the
agent runs doctor and the load". This ADR answers that question.

**Verified 2026-10-09** with a headless `claude -p` and a throwaway SessionStart hook:
- A hook process **does** see variables from settings `env` (`STDTEL_PROBE_SETTING=yes`,
  `CLAUDE_CODE_ENABLE_TELEMETRY=1`).
- `OTEL_*` variables are still scrubbed (`OTEL_LOGS_EXPORTER` unset), as ADR-003 records.

So a session can know, from inside, whether it started with Claude Code's own telemetry on.

## Options considered

1. **Instructions only.** Add "run `stdtel-doctor`, run `make load`" to `stdtel-setup`, CLAUDE.md and
   the analyst agent. **Rejected as the primary layer.** An instruction reaches only the agents that
   read it, at the moment they read it. The docusaurus session was set up correctly and still failed,
   because the failure happened after setup. An instruction cannot notice a condition nobody asked
   about. It stays, as the last layer (decision 6).
2. **Doctor as the only detector.** **Rejected as sufficient.** `stdtel-doctor` is the right home for
   the checks (decisions 1 to 3). But a doctor that runs only when someone runs it is the same gap
   one level down, so decision 4 puts the most important finding in the status line, where it is seen
   every turn, and ADR-017 gives the rest a page.
3. **Restart sessions automatically.** **Rejected.** A running session is the user's work. stdtel
   observes; it does not end what it observes (the same line berth draws: advisory, never kills).
4. **Auto-start a loader from a hook.** **Rejected** for the reason ADR-008 rejected auto-starting the
   stack: hooks run on every turn of every session, and a hook that launches long work couples the
   developer's latency to infrastructure.
5. **Mechanisms that surface the fault where the person already looks, with instructions on top.**
   Chosen.

## Decision

1. **Every health check has four outcomes, and only one of them is a pass.**
   - The outcomes are `pass`, `fail`, `pending` (too early to tell) and `unknown` (the evidence could
     not be read).
   - A check whose store is unreachable is `unknown`, never `pass` (ADR-005: an absent answer is not a
     clean one).
   - `stdtel-doctor` keeps its human output, and adds `--json`: one object per check with `name`,
     `outcome`, `observed`, `remedy` and `observed_at`.
   - Exit codes: `0` all pass; `1` any fail; `2` none failed but some are pending or unknown.
   - The status line, the analyst agent and the health page (ADR-017) read this contract and nothing
     else.

2. **`native coverage` is judged per session, with a grace period.**
   - Correlation key: the session id. A stdtel session file is named by the hook payload's
     `session_id`, which equals Claude Code's `session.id` on its events (verified 2026-09-30, and the
     key every join in ADR-014 already uses).
   - Population: sessions whose stdtel state was written in the last 24 hours and which have at least
     one completed turn more than 10 minutes old. The batch exporter's interval and Loki's ingestion
     lag are seconds, so 10 minutes is a wide margin.
   - Outcome per session:
     - `observed`: any `api_request` in Loki for it;
     - `missing`: none, past the grace period;
     - `pending`: inside the grace period;
     - `unknown`: Loki unreachable.
   - Loki retains 720 hours (`deploy/loki.yaml`), well past the 24-hour window. A retention shorter than
     the window makes the check `unknown`, not `missing`. *Implemented per session (#151): a session
     with no completed turn inside what Loki can still hold is `unknown`; one with a turn inside it
     is judged. Loki is asked about at most 7 days, so a months-long session never makes every run
     count months of events.*
   - Each `missing` session is named with its project and start time, and the remedy is "restart it;
     resuming is not enough".

3. **Data freshness and loader liveness are two checks, not one.**
   - **`warehouse freshness`** measures data only. Per loader, the watermark is the `source_max_ts` of
     its newest run with `ok = true`: the newest source timestamp it loaded, already recorded under
     ADR-009 decision 7. That watermark is compared with the newest event the source holds now.
     - It fails when the source holds events newer than the watermark by more than twice the interval
       (`STDTEL_LOAD_INTERVAL`, default 900 s).
     - A quiet source with an old but complete warehouse passes.
     - A successful run that loaded nothing while the source moved on fails.
   - **`loader liveness`** measures the scheduler only, and reads the **latest attempted run**,
     whatever its status.
     - It fails when that run is `ok = false`, naming its error, which covers a partial load
       (decision 5).
     - It also fails when no attempt has been made within twice the interval.
     - A recent failure is therefore reported even when an older success still makes the data look
       fresh.
   - **`unknown` is scoped per check.**
     - `warehouse freshness` is `unknown` when either Postgres or the source cannot be read, and when
       the loader has never had an `ok = true` run, so it has no watermark yet.
     - `loader liveness` needs only Postgres. A source outage never hides a failed latest attempt.
   - Rows loaded by a partial (`ok = false`) run do not advance the watermark. So freshness can fail
     even though some newer rows landed. That is the conservative choice, and it is deliberate.

4. **The session warns about itself, on every turn.**
   - At SessionStart the hook records in the session's state whether `CLAUDE_CODE_ENABLE_TELEMETRY` is
     in its environment (verified readable, Context).
   - It also resolves the settings that apply to this session, as Claude Code resolves them: project
     local, then project, then user. This is the same resolution doctor already uses for the endpoint.
     It persists one of three outcomes:
     - `enabled`: the resolved settings turn telemetry on;
     - `opted-out`: they resolve to off or unset, a deliberate choice at some level;
     - `unknown`: a settings file exists but cannot be read or parsed.
   - `enabled`, with the variable absent from the session's environment, means the session started
     before the setting and will not export.
   - **Channel:** `stdtel-statusline` already runs on every status refresh and reads session state. It
     shows "usage not exported: started before telemetry was on; restart to fix". It clears when a
     restarted session records the variable.
   - For `opted-out` nothing is shown: the user who chose it is not nagged.
   - For `unknown` nothing is shown either, because a warning that might be wrong every turn erodes the
     one signal that matters. The outcome is persisted, though, and doctor reports the unreadable
     settings file as its own finding.
   - A session already running when this lands has no record. The status line shows nothing for it,
     and `native coverage` (decision 2) is what names it.

5. **The loader runs on a schedule, and a partial load is a failed load.**
   - `stdtel-install loader` installs a per-user scheduler that runs the trace, request and delivery
     loaders every `STDTEL_LOAD_INTERVAL`:
     - on macOS, a launchd agent with `StartInterval`, which coalesces intervals missed during sleep
       into one run on wake;
     - on Linux, a systemd user timer with `Persistent=true`.
   - `stdtel-install loader --uninstall` removes it. Installing twice is a no-op.
   - The scheduler runs the installed `stdtel` with `STDTEL_DSN`, `STDTEL_TEMPO` and `STDTEL_LOKI` written
     into its own definition: a launchd or systemd job inherits no shell profile. Its output goes to
     `~/.stdtel/loader.log`, kept to the last 1 MB.
   - **One run at a time per machine.**
     - A lock directory (`~/.stdtel/loader.lock`) is created atomically and holds the owner's PID. It is
       removed on exit.
     - An overlapping run exits immediately.
     - A lock is reclaimed only when its PID is no longer alive, never on age alone, so a long but live
       run is never doubled.
     - The lock also records the owner's start time. A PID reused by an unrelated process is treated
       as dead, so a stale lock cannot become permanent.
     - A skipped run writes nothing; `loader liveness` (decision 3) notices runs that never happen.
     - *Implemented with an OS lock (#170): `flock` on `~/.stdtel/loader.lock`, which the kernel grants
       atomically and releases on the holder's exit. The directory, PID and start-time protocol above had
       race windows between creating the lock and naming its owner, and between judging it stale and
       removing it (Council, 7abf9a1f). The properties above hold without any staleness judgement.*
   - The compose `loader` profile (#121) remains the container option. Running both is detected by
     doctor and reported as one finding. They are not unsafe together: every insert is
     `ON CONFLICT DO NOTHING` on a stable key.
   - **Windows.**
     - A window wider than Tempo's 168-hour search maximum (`query_frontend.search.max_duration`) is
       split into half-open chunks `[start, end)` of at most that size, oldest first.
     - Rows are keyed on `span_id`, `(harness, request_id)` and `cr_id`, so a span seen at a chunk
       boundary, or by two runs, is written once. Nothing is double-counted.
     - `span_id` alone is kept as the key, not `(trace_id, span_id)`. stdtel's span ids are 64 random
       bits, so the chance of any collision among 10⁶ spans is about 3 × 10⁻⁸. Every join in the
       warehouse already uses `span_id`, and migrating the primary keys would buy almost nothing.
     - If any chunk fails, the run's `loader_run` row is `ok = false`, with the failing chunk in `error`,
       and the chunks already loaded stay loaded.
     - **There is no separate checkpoint.** The loaded rows are the progress. The next run re-queries
       the whole window, and the idempotent inserts skip what is already there. That costs a repeated
       query, never a duplicate row.
     - The maximum is read from Tempo's `/status/config` when available, and falls back to 168 h.

6. **Instructions are the last layer, and they point at the mechanisms.**
   - `stdtel-setup` installs the scheduler and ends by running doctor.
   - The analyst agent and `stdtel-query` run `stdtel-doctor --json` first. They state a non-pass
     `warehouse freshness` before quoting any warehouse number, and say how stale it is.
   - CLAUDE.md gains one line: before reporting from the warehouse, run `stdtel-doctor`.

### Work items

Each work item lists its acceptance tests.

- **W1. The check contract.** Covers decision 1: the four outcomes, `--json` and the exit codes.
  - Tests: one per outcome, for every existing check; exit code per outcome mix.
- **W2. `native coverage`.** Covers decision 2. Tests:
  - observed;
  - missing past the grace period;
  - pending inside it;
  - unknown with Loki down;
  - a session with no completed turn excluded;
  - retention shorter than the window gives unknown.
- **W3. `warehouse freshness` and `loader liveness`.** Covers decision 3. Tests:
  - fresh data with a recent run;
  - a quiet source with an old but complete warehouse: freshness passes, liveness fails;
  - a successful run that loaded nothing while the source moved on: freshness fails;
  - a recent partial run after an older success: liveness fails and names the error;
  - no attempt within twice the interval: liveness fails;
  - Postgres down gives unknown for both;
  - the source unreadable while Postgres is readable and the latest attempt failed: freshness unknown,
    liveness fails and names the error;
  - a loader with no successful run yet: freshness unknown.
- **W4. The session warns about itself.** Covers decision 4. Tests:
  - a session started without the variable while settings enable it warns;
  - settings that disable telemetry, at user, project or local level, persist `opted-out` and show
    nothing;
  - an unreadable settings file persists `unknown`, shows nothing, and doctor reports it;
  - a restarted session clears the warning;
  - a session with no record shows nothing.
- **W5. The scheduled loader.** Covers decision 5. Tests:
  - the install, reinstall and uninstall round trip, on both schedulers' definitions;
  - an overlapping run exits under the lock;
  - a lock whose PID is dead is reclaimed;
  - a lock whose PID is alive is never reclaimed;
  - a 192-hour window splits into two chunks;
  - a boundary span is written once;
  - a failing middle chunk gives `ok = false` and keeps the loaded chunks;
  - the log is bounded.
- **W6. The instruction layer.** Covers decision 6: `stdtel-setup`, the analyst agent, `stdtel-query`
  and CLAUDE.md. Tested as string checks (ADR-007), including that each names `stdtel-doctor --json`.

## Consequences

- A session that will not export says so on every turn of that session until it is restarted. A
  deliberate opt-out is left alone.
- A stale or incomplete warehouse is reported before anyone reads a number from it.
- `stdtel-install` gains a component that runs unattended. Its install, its removal and its log are
  all under `~/.stdtel` and documented, and nothing runs it unless the user installs it.
- The epic implementing this is the first one driven by `/loop /epic-loop`. Since #144 that loop is
  itself recorded as an `epic-loop` activation with trigger `loop`, which dogfoods the path.

### Known limitations

- **Lost usage stays lost.** The 295 docusaurus requests, and those of every session still running from
  before 2026-10-01, were never exported. Claude Code's records cannot be rebuilt afterwards. stdtel's
  own turn spans for those sessions hold transcript token counts but no cost. Spend panels undercount
  those sessions until they restart, and `native coverage` names them, so the gap is visible rather
  than silent.
- **Coverage needs Loki.** On a machine without the stack it is `unknown`, never a pass.
- **A session that predates this work has no SessionStart record**, so its status line stays quiet.
  Doctor names it.
- **Copilot is not covered.** stdtel's Copilot hooks do not yet run in every surface (#115), so these
  checks are Claude Code only.
- **The scheduler is per-user and per-machine.** A team deployment uses the compose profile or its own
  scheduler, and reads `warehouse freshness` centrally.

## Review

Council (llm-council, balanced tier, 3 of 4 models responding), 2026-10-09, on the first draft:
**rejected, confidence 0.78.** It agreed with the direction and with every rejected option. It found
that the mechanisms as written could recreate the failure the ADR exists to stop:
- freshness measured on run recency rather than data;
- partial chunked loads recordable as success;
- no correlation key, grace period or states for coverage;
- a SessionStart hook that fires once, behind a promise of a per-turn warning.

The second draft addressed each finding, in decisions 1 to 5 and in the work items' acceptance tests.
The compaction fix in the first draft was an accuracy bug, not a health mechanism, so it moved to its
own ticket.

Council on the second draft (3 of 4 models): **rejected, confidence 0.6, narrowly.** It found most
required changes met, with two remaining blockers:
- freshness still fused data with loader age;
- the check read the newest *successful* run, so a recent partial run was skipped.

It also asked for:
- a persisted settings outcome, enabled / opted-out / unknown;
- PID-based lock reclaim;
- stated resume semantics;
- a span key of `(trace_id, span_id)`.

This third draft:
- splits decision 3 into `warehouse freshness` (data watermark only) and `loader liveness` (the latest
  attempted run);
- persists the three-way settings outcome;
- reclaims the lock only by PID liveness;
- states that the loaded rows are the checkpoint;
- keeps `span_id` as the key, with the collision arithmetic stated in decision 5.

Council on the third draft (2 of 4 models): **approved, confidence 0.72.** Both blockers were resolved.
Its conditions are now applied:
- source unreadability scopes to freshness only, with the source-down, latest-failed test added;
- a never-loaded loader is `unknown` for freshness;
- PID reuse is handled by recording the owner's start time;
- the partial-run watermark asymmetry is stated.

