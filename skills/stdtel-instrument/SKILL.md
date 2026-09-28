---
name: stdtel-instrument
description: Make an external process report what it spent, so agent work can be costed end to end — reconcile its records against the provider's bill first, then emit std.artefact.activation spans with kind=external and prove them with stdtel-conform. Use when a tool an agent shells out to, or an MCP server, spends money on models and that spend is invisible in telemetry.
license: MIT
metadata:
  version: "1.1.0"
  standard_id: STD-TEL-001
  policy_ids: ""
  owner: platform-observability
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: manual
---

# Instrument an external process

An agent shells out to a tool, or calls an MCP server, and that process spends real money on models of
its own. The harness sees a command ran. It cannot see the amount. Only the spending process can report
it, which is what [ADR-010](../../docs/adrs/010-containment-and-scope.md)'s `external` kind is for.

Your output is a **pull request against somebody else's repository**, in their style, with their tests.
Not a metadata block. Budget accordingly.

## Step 1 — reconcile before you touch any code

Do not start by reading the emitting code. Start by asking whether the records that already exist
account for the money that was actually spent.

1. Find the local record, if any. Sum its costs over a period.
2. Get the provider's billed total for the same period.
3. Compare them.

**A gap is the finding, and its shape tells you what to fix.** A record that is complete but small
means a whole code path is unrecorded. A record that is patchy across the board means capture is
failing on some responses. A record that is patchy in *blocks* — whole sessions all-or-nothing — means
a code path or an epoch, not a flaky field.

Then check what is polluting the file before you trust any ratio. Test fixtures written to a real store
path, or rows from before the cost field existed, will make a working pipeline look broken.

This step is not ceremony. On the first real use of this skill, the naive read of the data said cost
capture was two-thirds missing. It was not: capture was complete for every path that recorded at all,
a quarter of the file was test fixtures, and the actual defect was that the expensive path wrote no
record whatsoever. Every hour spent on the wrong fix started with skipping this step.

**Write down what you reconciled, including what you could not.** The number you cannot explain is the
most valuable line in the ticket.

## Step 2 — find the one place every call goes through

You want one span per unit of work, not per HTTP request. Look for the boundary where a usage summary
already exists — most projects that track cost at all have aggregated it somewhere before writing it
down.

Then check for bypasses. A facade that *most* calls use is not a choke point; find the imports that
skip it. A span emitted from a layer two callers avoid is a total that is quietly short.

## Step 3 — emit

One span per unit of work, named `std.artefact.activation`:

| attribute | value |
|---|---|
| `std.artefact.kind` | `external` |
| `std.artefact.name`, `std.external.system` | the system's name, bounded |
| `std.external.operation` | a bounded verb from its own vocabulary |
| `std.external.cost_usd` | number — what was **observed**; **omit when not observed** |
| `std.external.cost_source` | `provider` or `local` — how `cost_usd` was observed |
| `std.external.cost_estimated_usd` | number — what was priced from a list, never billed |
| `std.external.requests`, `std.external.duration_ms` | counts |
| `std.artefact.source` | `emitter` — you reported this about yourself |
| `gen_ai.request.model`, `gen_ai.usage.*` | model and token counts |
| `session.id` | the **Claude** session, from `CLAUDE_CODE_SESSION_ID` |

`stdtel-conform --print-contract` prints this list as JSON, with a `contract_version`. Diff your own
copy against it in CI to *detect* drift — do not generate your copy from it. Both sides keep a longhand
list precisely so that neither is comparing a list with itself, and a copy pulled at build time lets a
rename flow straight through.

**Split the amount by provenance; do not label one figure.** One run can mix billed calls with calls
priced from a price list. `cost_usd` carries only what was observed and `cost_source` says how:
`provider` when a provider billed it, `local` when the model is self-hosted and no bill exists.
Whatever was estimated goes in `cost_estimated_usd`, beside it, never inside it. A run that was *only*
estimated sends `cost_estimated_usd` and no `cost_usd` at all. The receiving warehouse measures
coverage against an invoice from `cost_usd` alone, so an estimate folded into it becomes a bill once
summed — which is the reason an emitter with no way to label estimates had to drop them entirely. The
estimate vocabulary is yours to keep internally; `registry_estimate` or `local_zero` are your names for
your code paths, not values for the contract.

`source` records *how the value was come by*, and the other two values are not yours to use: `hook`
means the Claude Code harness handed it over and `transcript` means stdtel inferred it from a session
file. Nothing outside your process saw your spend, so stamping either would assert an observation that
never happened. `stdtel-conform` rejects it.

Five rules that are not negotiable, each because breaking it produces a number that looks right:

- **Metadata only.** No prompt, no response, no question, no arguments, no file list. The payload is
  not the receiving project's to scrub.
- **Omit an unobserved cost. Never send zero or null.** A zero is a measurement — free tiers and cached
  responses really do cost nothing. An absence is not. Collapsing them makes every average wrong. The
  same holds for `cost_estimated_usd`.
- **Never put an estimate in `cost_usd`.** It is the one figure that reconciles to an invoice.
- **`session.id` is the harness's id, not the process's own.** A local run id joins to nothing. If
  `CLAUDE_CODE_SESSION_ID` is unset, omit the attribute. Such a run lands with an empty session and is
  attributable to system and operation only — never to a session, turn or scope, because an empty
  session joins to every other empty session.
- **Emit anyway when there is no session.** A standalone or CI run is real spend, and a span never
  emitted is a total that can never reconcile.

**Which endpoint.** Honour the standard `OTEL_EXPORTER_OTLP_ENDPOINT` (and
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`), nothing project-specific. stdtel's own `STDTEL_*` variables exist
only because Claude Code scrubs `OTEL_*` from *hook* processes (ADR-003).

**For an MCP server, the `env` block is the only route that works.** Verified 2026-09-28 on Claude
Code 2.1.283 with a stub stdio server that recorded its environment: an `OTEL_EXPORTER_OTLP_ENDPOINT`
exported in the launching shell was **removed** before the server started, while an ordinary inherited
variable beside it arrived intact; the same variable set in the server's `env` block in the MCP
configuration arrived as set. Exporting it in a shell profile therefore does nothing, silently. The same
run showed `CLAUDE_CODE_SESSION_ID` *is* passed to MCP servers, so reading `session.id` from it works.
A local stack listens for OTLP over HTTP on `http://localhost:4318`.

**No endpoint configured must mean no exporter, no network call and no added latency.** The process
must behave exactly as it does today for everyone who has never heard of this. An unreachable endpoint
must not block or slow a run either.

## Step 4 — prove it, in their CI

```bash
stdtel-conform spans.json        # OTLP JSON: resourceSpans, or Tempo's batches
```

It fails on a wrong span name, an attribute outside the allowlist, anything carrying content, a missing
system, a malformed session id, a cost that is not a number, or a `cost_source` outside the vocabulary
or with no observed cost to describe. Separately it reports **how many spans carry an observed cost at
all**, and how many carry only an estimate — because a shape-only check passes a file whose costs are
missing, and the warehouse then averages over the holes.

**Their own test is the gate; `stdtel-conform` is a second opinion.** Their allowlist lives in one
constant, and their test compares it longhand, both ways, against the published set. `stdtel-conform`
catches drift between that constant and the receiver's. If it is unavailable, lagging or wrong, their
build must still fail correctly without it. Wire both into CI against a dumped span from a real run. A
check that a human remembers to run is not a check.

**How the contract changes.** Additively, and announced before it merges: the receiving repo pins the
published set in a test that fails on any edit, and says in that failure to announce the change first.
`contract_version` increases on every change, additions included, so a diff against
`--print-contract` tells an emitter something new exists that it does not yet send.

The receiver's loader also names any attribute on an external span that it does not read, on every run,
and records the count. A renamed attribute therefore shows up as a named key, not a column of NULLs.

## What good looks like

- [ ] The reconciliation is written down, including the part that does not add up.
- [ ] One span per unit of work, from a place no caller bypasses.
- [ ] Their own contract test gates the build; `stdtel-conform` passes beside it with complete
      observed-cost coverage, and every estimate travels in `cost_estimated_usd`.
- [ ] A test greps every attribute of every span for a recognisable string put through a real run, so
      a content leak fails the build rather than reaching a collector.
- [ ] With no endpoint set, behaviour and latency are unchanged — asserted, not assumed.
- [ ] Their own conventions are met: their ADR practice, their docs, their definition of done.

## What this skill will not do

Guess a cost. If the process cannot observe what it spent, the fix is upstream of any telemetry, and
emitting an estimate that cannot be told from a measurement is worse than emitting nothing — it will be
summed with real figures by someone who does not know.
