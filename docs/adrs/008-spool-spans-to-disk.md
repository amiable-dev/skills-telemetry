---
title: "ADR-008: Spool spans to disk; the hook never opens a socket"
status: proposed
date: 2026-09-12
tags: [adr, hooks, reliability, telemetry]
links: ["003-hook-execution-constraints.md", "005-data-integrity.md", "../for-developers.md"]
tracking: "https://github.com/amiable-dev/skills-telemetry/issues/14"
---

## Context

`stop` exports over OTLP synchronously. When the collector is unreachable the exporter waits out
`STDTEL_OTLP_TIMEOUT` and drops the spans, leaving one stderr line that no editor shows. Measured
before the timeout was bounded: **7.34s of retry backoff per turn**; bounded, 0.91s and the data gone.

That is three problems in one. Data is lost whenever the collector is down — offline, before `make up`,
during a collector restart. The developer's hook latency depends on a network service being healthy.
And "hooks never block the developer" currently holds only because we chose a small timeout, not
because the hook is structurally incapable of blocking.

The question that surfaced this was whether the stack should start automatically when the plugin is
installed. It should not, and the reasons are worth recording because they will be asked again:
the collector is shared infrastructure in any real deployment, `docker compose up` takes seconds to
minutes against a ~40ms hook budget, Docker may be absent, and a telemetry plugin silently starting
eleven containers contradicts what [`../for-developers.md`](../for-developers.md) promises. Auto-start
treats the symptom; the coupling is the disease.

## Options considered

- **Start the stack automatically.** Rejected, as above. It also only helps people running a local
  collector, which is the development case, not the deployed one.
- **Raise the export timeout.** Rejected: it trades silent loss for a slow editor, and the hook still
  cannot succeed offline.
- **Retry in the hook.** Rejected: a hook is a short-lived process invoked per event; there is nowhere
  to retry *to*, and holding the turn open to retry is the thing we must not do.
- **Rely on a local collector as the buffer** (what `otel-cli` recommends). Rejected as the primary
  answer for the same reason as auto-start: it requires everyone to run infrastructure. It remains a
  fine deployment choice for those who do.
- **Batch in memory across hook invocations.** Not possible: each hook is a separate process. This is
  the same constraint that made `SessionState.save()` have to list every field.

## Decision

1. **`stop` appends NDJSON to a spool under `~/.stdtel/` and returns. No socket is opened in a hook.**
   A hook that never talks to the network cannot stall on it, which is what "never block the developer"
   was always reaching for.
2. **A separate process drains the spool** — `stdtel-export --once` for a scheduler or `make up`, and
   `--watch` for a long-running drain. Export failure leaves records in place.
3. **Records are deleted only after a successful export**, and the drain is idempotent: a crash
   mid-drain must neither lose nor duplicate.
4. **The spool is bounded** by size or age, oldest dropped first, **and drops are counted**. Unbounded
   growth on a developer's disk is not acceptable, and silent loss at the bound would reintroduce
   exactly the failure this ADR removes ([ADR-005](005-data-integrity.md)).
5. **`scrub()` runs before anything is written.** The content rules apply to disk, not only to the wire.

## Consequences

- Capture works offline and across collector restarts. Turning the stack on later collects what already
  happened rather than starting from now.
- The hot path loses its only network call; `stop` approaches the interpreter floor.
- The failure mode moves from "data lost, silently" to "data queued, visibly" — which is the point.
- New state appears on the developer's disk, so it must be documented where they will look and covered
  by the uninstall instructions.

### Known limitations

- **A second moving part.** If nobody runs the drain, the spool fills and then drops at the bound. That
  is more visible than today's loss but it is still loss, and it is a new operational duty. `stdtel
  doctor` (#15) should report a spool that is not draining.
- **Ordering and the `tail_tokens_first_only` sensitivity check** assume spans arrive as emitted. The
  drain must preserve order per session.
- Spooling does not make the data more correct, only more durable. Everything in
  [`../evaluation-power.md`](../evaluation-power.md) about sample size is unaffected.
- Anyone already running a local collector gains little; for them the collector is the buffer.

## Related

Issue #14 tracks implementation. #15 (`stdtel doctor`) is how a stalled spool becomes visible, and #16
(statusline) is how it reaches the developer while they can still act on it.
