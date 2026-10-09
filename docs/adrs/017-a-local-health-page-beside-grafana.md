---
title: "ADR-017: A local health page beside Grafana, not instead of it"
status: accepted
date: 2026-10-09
tags: [adr, dashboard, grafana, ui, health]
links: ["006-langfuse-as-an-optional-trace-backend.md", "015-telemetry-health-is-a-mechanism.md", "016-counting-rare-events.md"]
---

## Context

The maintainer asked whether stdtel should have its own dashboard, like berth's, instead of or as
well as Grafana.

berth's dashboard (`berth ui`, `~/port-visualiser`) is a zero-dependency local server.
- It binds 127.0.0.1 and serves GET only.
- It serves one page, inlined in the bundle, so nothing is served from disk.
- It refuses cross-site requests to its one JSON endpoint.
- It reads the CLI's own state.

It answers one question well: who holds what, right now, on this machine.

Grafana here answers a different kind of question: aggregates over the stores (Prometheus, Loki,
Tempo, the warehouse). It does that adequately once its sources are right (ADR-016).

The deciding constraint is **what each surface can see.** The faults ADR-015 found were invisible in
Grafana because their evidence is not in any store Grafana reads:
- **which sessions are running** and when each started;
- **what the settings files say;**
- **what `~/.stdtel/sessions` holds;**
- **whether the loader's scheduler is installed.**

An empty panel cannot say "this session never exported". Only something that can read the machine
can say it.

## Options considered

1. **Grafana only, with health as `stdtel-doctor` output.** **Rejected as complete.** Doctor is the
   right source of truth (ADR-015), but it is text in a terminal. A page is also somewhere a person has
   to go, so discoverability is not the argument. The argument is that the per-session warning in the
   status line (ADR-015 decision 4) tells someone to look, and when they do, the answer needs
   somewhere to live. That answer is a list of sessions, their stores and the loader's lag, with
   remedies.
2. **Replace Grafana with a purpose-built UI.** **Rejected.** Grafana already does time series,
   LogQL, TraceQL and SQL over four stores, with time ranges, drill-down and alerting. Rebuilding that
   would be the larger part of the project for no new answer, and it would take away the surface a team
   deployment already understands.
3. **Health panels inside Grafana, fed by a doctor endpoint.** **Rejected.** A Prometheus-scraped
   health endpoint is possible, but it adds a pipeline whose freshness is itself in question. It
   flattens a check's remedy and its named sessions into labels. And it still could not show a remedy
   as an instruction. The page can link to Grafana; Grafana cannot usefully host a list of named
   faults with what to do about each.
4. **A small local health page beside Grafana, in berth's shape.** Chosen. It shows what only the
   machine can see, and it links into Grafana for everything that is an aggregate.

## Decision

1. **`stdtel-ui` serves one local page.**
   - It binds 127.0.0.1 on this project's berth `web` port. `--port` overrides it, and if the port is
     held the page refuses to start rather than picking another (berth's rule).
   - It is a foreground process with no auto-start. Ctrl-C ends it, and it holds no state of its own.
   - Following berth's rules: GET only; the page inlined in the bundle; nothing served from disk; a
     per-response CSP with `frame-ancestors 'none'`; cross-site requests to its JSON endpoint refused.
   - **Host header validated** against `127.0.0.1:<port>` and `localhost:<port>` exactly, which is the
     defence against DNS rebinding. Anything else gets 421.
2. **Data access: the page runs doctor's checks and nothing else.**
   - The server calls the same check functions as `stdtel-doctor` and returns their typed results
     (ADR-015 decision 1: `name`, `outcome`, `observed`, `remedy`, `observed_at`).
   - It reads only what doctor reads: local files under `~/.stdtel` and `~/.claude`, and the stores
     doctor already queries, on loopback, with the credentials doctor already has. There is no spend
     figure, and no aggregate of any store: those are Grafana's (decision 4).
   - **Bounded execution.** Each check runs under its own timeout, and its result is cached for a
     per-check TTL (cheap file checks 15 s; store queries 60 s). Concurrent requests share one run.
     A check that times out or raises is `unknown`, with the reason, and never blocks the others.
   - The page polls every 30 s and shows each check's `observed_at`, so a stale result looks stale.
3. **Everything rendered is escaped, and nothing sensitive is rendered.**
   - Check text, session ids, skill names and paths are inserted as text nodes, never HTML.
   - Paths under the home directory render as `~`.
   - Environment and settings values are never rendered. A setting appears only as the fact it implies
     ("telemetry enabled: yes").
4. **Grafana owns aggregates, and the page links to them.**
   - Each finding that has an aggregate behind it links to the provisioned dashboard by its uid, with
     the time range and the session or skill as URL-encoded variables.
   - The page holds no chart Grafana already draws.
5. **One implementation.** A check added to doctor appears on the page with no page code; a test holds
   that.
6. **Grafana remains the primary analysis surface**, and the one a team deployment uses. ADR-006 is the
   precedent for an optional surface beside a primary one.

### Work items
- **W1. `stdtel-ui` server and page.** Covers decisions 1 to 3. Tests:
  - Host validation, including a rebinding attempt returning 421;
  - GET-only;
  - the CSP header;
  - cross-site refusal;
  - port-held refusal;
  - a check timing out rendered as `unknown` while the others render;
  - concurrent requests coalescing to one run;
  - a hostile check string rendered as text;
  - `~` redaction;
  - no setting value rendered.

  The page design starts from berth's handoff pack.
- **W2. Deep links and the shared implementation.** Covers decisions 4 and 5. Tests:
  - every link resolves to a provisioned dashboard uid and variable;
  - identifiers are URL-encoded;
  - a check registered in doctor appears on the page.

## Consequences

- A person sees "this session exports nothing" and "the warehouse is a week old" without asking.
- stdtel gains a long-running process the user starts. It is optional, local, and holds no state of its
  own.
- Two surfaces, with a rule for which owns what: the page owns the machine, Grafana owns the stores.

### Known limitations

- **Per-machine only.** The page sees one machine's sessions. A team's health still needs doctor run on
  each machine, or the scheduled loader's freshness read centrally.
- **Unverified design fit.** berth's visual language was built for a port map. Reusing its design pack
  for health checks is a starting point, not a decision about how health should look.
- **It needs the web port free.** If something else holds it, the page refuses to start rather than
  picking another (berth's rule, kept). `--port` is the override.
- **Session ids are listed.** They are opaque, and the page binds only to loopback, but anyone with a
  shell on the machine can read them. That is the same exposure `~/.stdtel/sessions` already has.

## Review

Council (llm-council, balanced tier, 2 of 4 models responding), 2026-10-09, on the first draft:
**approved in direction, with amendments required.** It endorsed the boundary (the page owns the
machine, Grafana owns the stores). It asked for:
- an explicit data-access model;
- removal of the Loki spend figure, a scope leak into Grafana's territory;
- Host-header validation against DNS rebinding;
- output escaping and redaction;
- bounded, cached check execution with explicit `unknown` and stale states;
- a typed result contract;
- tested deep links, and a stated process lifecycle.

It also judged the first draft's reasons for rejecting options 1 and 3 too weak, so both are restated.
This revision makes each change.

Council on the revision (all 4 models): **approved, confidence 0.85, no remaining blocking gaps.**
Staleness shown as `observed_at` plus a TTL, rather than a separate outcome, was judged adequate.
