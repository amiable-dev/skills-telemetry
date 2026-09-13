---
title: "ADR-003: Hook execution constraints — what the harness environment forces on us"
status: accepted
date: 2026-09-10
tags: [adr, hooks, runtime, opentelemetry]
links: ["001-distribution-and-capture-surface.md", "005-data-integrity.md", "../../stdtel/hooks/cli.py", "../../stdtel/exporter.py"]
verified: "2026-09-10 against Claude Code 2.1.267 via a headless `claude -p` run with capture hooks; payloads committed as tests/fixtures/hook_payloads.json"
---

## Context

A hook is not an ordinary program. It runs in an environment chosen by the harness, and four
properties of that environment are non-negotiable and were each discovered the hard way:

1. **`OTEL_*` is stripped from every subprocess Claude Code spawns.** Quoted from the hooks reference:
   "A hook process inherits the parent environment, apart from the `OTEL_*` exporter variables that
   Claude Code removes from every subprocess it spawns."
2. **The shell is `sh -c`, not a login shell**, and PATH is inherited from whatever launched the
   harness. Version managers (mise, asdf, pyenv) and activated venvs are therefore absent.
3. **Hooks are on the developer's critical path.** `PreToolUse`/`PostToolUse` fire per tool call, at
   roughly 30 calls per prompt.
4. **Hooks exit 0 by design** so telemetry never blocks the developer — which makes silent failure
   the default failure mode. That consequence is [ADR-005](005-data-integrity.md).

## Options considered

- **Invoke `stdtel-hook` by bare name and rely on PATH.** Rejected: unresolvable whenever a version
  manager put it there, which is the common case. Every comparable tool — pre-commit, lefthook, trunk
  — bakes an absolute interpreter path at install time instead.
- **Configure the collector endpoint with `OTEL_EXPORTER_OTLP_ENDPOINT`.** Rejected: scrubbed before
  the hook runs. Worse than broken — the SDK falls back to `http://localhost:4318`, so a team pointing
  at a real collector gets a working-looking pipeline that silently writes nowhere.
- **Bound the flush with `force_flush(timeout_millis=...)`.** Rejected: ignored upstream
  ([opentelemetry-python#4043]). Measured: 7.63s with a 500ms argument.
- **Bound it with `OTEL_EXPORTER_OTLP_TIMEOUT`.** Rejected: it is an `OTEL_*` variable, so it is
  scrubbed too. This is the trap — it is the documented lever and it cannot reach us.
- **Emit one span per tool call.** Rejected: ~30 spans per prompt for a metric that needs counts.
- **Import OpenTelemetry at module scope.** Rejected on measurement: 40ms per hook versus 20ms with
  function-local imports, against a 10ms interpreter floor. Semgrep pays 117ms per invocation to this
  exact mistake.
- **Determine the harness from `STDTEL_HARNESS` alone.** Rejected: VS Code Copilot reads
  `~/.claude/settings.json`, so Copilot events arrive through hooks registered for Claude Code and
  would be stamped `claude-code` — silently corrupting the cross-harness comparison.

## Decision

1. **Configuration uses a `STDTEL_` namespace** — `STDTEL_OTLP_ENDPOINT`, `STDTEL_OTLP_TIMEOUT` —
   which survives the scrub. `OTEL_*` is honoured as a fallback for direct CLI and CI use, where
   nothing scrubs it.
2. **The flush timeout is set on the exporter constructor**, the only lever that works. Measured:
   7.34s unbounded versus 0.91s at 2s.
3. **Hooks are invoked by absolute path**, resolved at install time by `stdtel-install` from the
   console script beside its own interpreter. Verified to run under `env -i`.
4. **Every `stdtel` import in `hooks/cli.py` is function-local**, module scope stays stdlib-only, and
   `pre_tool_use` does not load the catalogue (Stop resolves the version anyway). A regression test
   asserts `opentelemetry` and `yaml` stay out of `sys.modules`.
5. **`PreToolUse` stays matched to `Skill`; `PostToolUse` is unmatched.** Failure rate needs every
   tool; opening skill windows does not.
6. **Harness detection uses camelCase keys as positive evidence of Copilot**, falling back to
   `STDTEL_HARNESS`. Copilot's snake_case dialect is indistinguishable from Claude Code's, so its
   hooks must set `STDTEL_HARNESS` in their own `env` block.
7. **Identity is derived, never generated** (`stdtel/identity.py`). A value the SDK generates per
   process is a new value per hook, and `service.instance.id` becomes Prometheus's `instance` label
   — so every span opened its own series, each counter reached 1 and stopped, and every `rate()`
   panel read zero at any volume (#42). `service.instance.id` and `std.user.hash` are both SHA-256
   of values that exist under `env -i`: the hostname, and the uid with the hostname.

## Consequences

- The exporter is unreachable by standard OTel configuration, which will surprise anyone who knows
  OTel. Documented at every point where someone would try.
- Hook cost is ~20ms against a 10ms floor, roughly 1s per interaction at 30 tool calls. Acceptable
  because it overlaps multi-second model turns, but it is not free and must not regress.
- A distributed hook manifest cannot carry the absolute path, so `stdtel-install` must run after any
  plugin install. Shipped manifests carry the bare name and are explicitly a step short of working.
- Separate processes also mean **nothing can be held in memory between events**, and that anything
  the harness decides at session start — hook registration above all — is fixed for that session's
  life. An install never reaches an already-open session, which is invisible because hooks exit 0
  (#44). `stdtel-install` says so, and `stdtel-doctor` reports which project its newest data came
  from rather than that data exists.

### Known limitations

- **Copilot in snake_case mode cannot be distinguished from Claude Code by payload alone.** Detection
  is positive evidence only; the ambiguous case needs configuration, and a misconfigured fleet will
  mislabel silently. There is no fix available at the payload level.
- **Detection returns `copilot`, not `copilot-vscode` or `copilot-cli`** — the payload does not say
  which, so the finer label needs `STDTEL_HARNESS`.
- The 2s default flush timeout **drops telemetry rather than delaying the developer** when a collector
  is slow. That trade is deliberate and stated, but it is a trade.
- `${CLAUDE_PLUGIN_ROOT}` was not used: it is documented but unexercised here, and the absolute-path
  approach is verified.

## Related

Verified against a live session; the captured payloads are replayed in `tests/test_live_payloads.py`.
That run also settled that `caller` is **absent** from the hook payload, which is why trigger comes
from the transcript ([ADR-005](005-data-integrity.md)).

[opentelemetry-python#4043]: https://github.com/open-telemetry/opentelemetry-python/issues/4043
