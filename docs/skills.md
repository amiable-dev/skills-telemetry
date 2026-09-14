# Skills and agents

What ships, when to reach for each, and — more usefully — when not to.

A skill's own body tells the *model* how to behave. It is not user documentation and does not say when
a human should invoke it. That is what this page is for.

## Installing them

Skills and agents are both **loaded at session start**, so a running session will not see something
you just added.

```bash
# as a plugin (both skills and the agent)
/plugin marketplace add amiable-dev/skills-telemetry
/plugin install stdtel@amiable-standards

# or locally
ln -s "$PWD/skills/stdtel-query" ~/.claude/skills/stdtel-query
mkdir -p .claude/agents && cp agents/skill-scorecard-analyst.md .claude/agents/
```

For telemetry to attribute them, the catalogue must also see them — see `STDTEL_SKILLS_ROOT` in
[reference.md](reference.md#environment-variables). A skill the catalogue cannot find still emits
spans, as `unversioned` with no `standard_id`.

---

## `skills/stdtel-onboard`

**What** — rewrites an existing `SKILL.md` into the standards-telemetry contract: contract fields
under `metadata:`, a `standard_id`, `policy_ids`, and validation.

**Use it when** a skill shows up as `unversioned` in the data, `stdtel-validate` fails, or you are
adding a skill to the catalogue for the first time.

**Do not use it** to make a validator pass. It will tell you to set `success_signal: manual` when no
policy actually verifies the skill, rather than pointing `policy_ids` at an unrelated policy — a skill
whose effect cannot be checked should say so.

**Refuses to** invent a `standard_id` or `policy_ids` that do not reflect what the skill body says.

## `skills/stdtel-query`

**What** — answers cost and outcome questions against Postgres, Tempo and Prometheus, with the SQL for
the primary ratios.

**Use it when** you want a number: what a skill cost, cache-hit rate, tokens per merged PR, tool-call
failure rate, or a specific session's spans.

**Do not use it** for keep/deprecate decisions — that is the agent's job, and it involves a judgement
about sample size this skill does not make.

**Refuses to** report a comparison below the sample-size floor, and always reports row counts beside
any figure. Knowing which denominator a ratio uses is half of what it teaches: `session_cost` is total
spend, `skill_invocation.tail_tokens` is the share one skill could claim, and they must never be summed.

## `skills/structured-logging`

**What** — the worked example of a *standard* as a skill, and the subject of the eval fixture. Emits
JSON logs with the platform's required fields and no PII.

**Use it when** demonstrating the loop end to end, or as the template for a real standard.

**Note** it is an example, not a production logging standard for your platform. `STD-LOG-001` and its
two policies exist to make the with/without evaluation runnable.

---

## `agents/skill-scorecard-analyst`

**What** — reads the telemetry and recommends keep / refine / merge / deprecate per skill, with the
evidence behind each call.

**Use it when** you have enough data to ask "which of these skills is worth keeping?" — see the phases
in the [README](../README.md#when-can-i-trust-these-numbers).

**Do not use it** as a query tool; use `stdtel-query`. And do not use it to justify a decision already
made — it is built to refuse.

**Refuses to** make a comparative claim below the floor: *below 30 merged PRs per arm, or fewer than 5
developers, report descriptively and make no comparative claim.* That refusal is the correct answer,
not a failure to answer. Pressed for one anyway, it gives the cost picture and states the volume that
would be needed.

It also knows this dataset's traps: `unversioned` means *not catalogued* (recommend onboarding rather
than reporting a finding), `unattributed` rows are kept for cost and excluded from outcome, and a wide
gap between `tail_tokens` and `tail_tokens_first_only` means the per-skill split is an assumption
rather than a measurement.

---

## Attributing a skill you do not own

Editing somebody else's `SKILL.md` to add the `metadata:` block works exactly once. The next upstream
release overwrites it; a plugin reinstall replaces the whole directory; a new machine has none of it.
Each of those is silent — the skill keeps emitting spans, they just arrive `unversioned`, which looks
identical to a skill nobody has onboarded yet.

Two routes avoid touching the file. Pick by who needs the answer.

### One developer, or skills only you use: an overlay root

```bash
export STDTEL_SKILLS_OVERLAY=~/skills-overlay      # a directory you control, in git
```

Each entry is a stub with front-matter and **no body**:

```yaml
---
name: graft                         # must match the skill's own front-matter name
metadata:
  standard_id: STD-CTX-001
  policy_ids: ""
  owner: platform
  telemetry.success_signal: manual
---
```

An overlay **fills gaps and never overrides**. If the skill states its own `version`, the skill wins —
so the day upstream starts declaring one, your data follows it instead of reporting a pinned value
that stopped being true. That is the opposite of `STDTEL_SKILLS_ROOT`, where the earliest root wins
outright.

A stub may omit `version`: you usually have no honest one for someone else's artifact.
`std.skill.content_hash` identifies it instead, and unlike a version it cannot go stale.

> Do not put the overlay inside a directory the harness scans for skills — `~/.claude/skills` above
> all. The stubs would become empty skills offered to the model.

### A team: the collector

`collector/copilot-skill-map.yaml` is the same idea applied centrally: a name → contract table the
collector uses to fill `std.*` attributes that arrived without them. One table, held once, applying to
everyone, surviving every client change, and requiring nothing installed on a developer's machine. It
already uses the correct `where attributes[...] == nil` guard, so it fills gaps rather than
overriding.

Prefer this whenever more than one person needs the attribution. An overlay root is per machine and
will be forgotten on the next one.

## Opting a skill out

A skill author can suppress named attribution for their skill:

```yaml
metadata:
  telemetry.emit: "false"
```

Its tokens still count toward the session total, which carries no skill name. To switch telemetry off
for yourself entirely, see [for-developers.md](for-developers.md).

## Which one do I want?

| question | reach for |
|---|---|
| "what did this cost?" | `stdtel-query` |
| "should we keep this skill?" | `skill-scorecard-analyst` |
| "why is this skill `unversioned`?" | `stdtel-onboard` |
| "how do I install the hooks?" | [reference.md](reference.md) |
| "how do I reach Grafana / Tempo / Postgres?" | [local-stack.md](local-stack.md) |
| "can I trust this number yet?" | [evaluation-power.md](evaluation-power.md) |
| "what is collected about me?" | [for-developers.md](for-developers.md) |
