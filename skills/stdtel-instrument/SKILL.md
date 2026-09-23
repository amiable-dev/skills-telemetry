---
name: stdtel-instrument
description: Make an external process report what it spent, so agent work can be costed end to end — reconcile its records against the provider's bill first, then emit std.artefact.activation spans with kind=external and prove them with stdtel-conform. Use when a tool an agent shells out to, or an MCP server, spends money on models and that spend is invisible in telemetry.
license: MIT
metadata:
  version: "1.0.0"
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
| `std.external.cost_usd` | number — **omit when not observed** |
| `std.external.requests`, `std.external.duration_ms` | counts |
| `gen_ai.request.model`, `gen_ai.usage.*` | model and token counts |
| `session.id` | the **Claude** session, from `CLAUDE_CODE_SESSION_ID` |

Four rules that are not negotiable, each because breaking it produces a number that looks right:

- **Metadata only.** No prompt, no response, no question, no arguments, no file list. The payload is
  not the receiving project's to scrub.
- **Omit an unobserved cost. Never send zero or null.** A zero is a measurement — free tiers and cached
  responses really do cost nothing. An absence is not. Collapsing them makes every average wrong.
- **`session.id` is the harness's id, not the process's own.** A local run id joins to nothing. If
  `CLAUDE_CODE_SESSION_ID` is unset, omit the attribute.
- **Emit anyway when there is no session.** A standalone or CI run is real spend, and a span never
  emitted is a total that can never reconcile.

**No endpoint configured must mean no exporter, no network call and no added latency.** The process
must behave exactly as it does today for everyone who has never heard of this. An unreachable endpoint
must not block or slow a run either.

## Step 4 — prove it, in their CI

```bash
stdtel-conform spans.json        # OTLP JSON: resourceSpans, or Tempo's batches
```

It fails on a wrong span name, an attribute outside the allowlist, anything carrying content, a missing
system, a malformed session id, or a cost that is not a number. Separately it reports **how many spans
carry a cost at all** — because a shape-only check passes a file whose costs are missing, and the
warehouse then averages over the holes.

Wire it into their CI against a dumped span from a real run. A check that a human remembers to run is
not a check.

## What good looks like

- [ ] The reconciliation is written down, including the part that does not add up.
- [ ] One span per unit of work, from a place no caller bypasses.
- [ ] `stdtel-conform` passes in their CI with complete cost coverage.
- [ ] A test greps every attribute of every span for a recognisable string put through a real run, so
      a content leak fails the build rather than reaching a collector.
- [ ] With no endpoint set, behaviour and latency are unchanged — asserted, not assumed.
- [ ] Their own conventions are met: their ADR practice, their docs, their definition of done.

## What this skill will not do

Guess a cost. If the process cannot observe what it spent, the fix is upstream of any telemetry, and
emitting an estimate that cannot be told from a measurement is worse than emitting nothing — it will be
summed with real figures by someone who does not know.
