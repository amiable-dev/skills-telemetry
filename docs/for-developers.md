# What this collects, and how to switch it off

This runs on your machine, in your editor, on every tool call. You are entitled to know exactly what
leaves it and to stop it without asking anyone. This page is that.

If you only read one thing: **`export STDTEL_DISABLED=1` turns it off completely**, and it takes
effect before your transcript is even opened.

## What leaves your machine

One span per **artefact activation** and one per session. An artefact is one of four things, and the
span says which in `std.artefact.kind`:

| kind | one span each time | added |
|---|---|---|
| `skill` | a skill is invoked | from the start |
| `subagent` | a sub-agent finishes | 2026-09, ADR-009 |
| `compaction` | your context is compacted | 2026-09, ADR-009 |
| `turn` | you send a prompt and the assistant finishes replying | 2026-09, ADR-009 |

The three new kinds were added because a real session of this project's own cost $366, spawned 42
sub-agents and compacted twice, and none of that was recorded — the numbers that were being collected
could not answer "where did it go".

Every kind carries only these fields:

| field | example | where it comes from |
|---|---|---|
| skill name, version, plugin | `structured-logging`, `2.3.0`, `epic-loop` | the skill you invoked |
| standard id, policy ids | `STD-LOG-001` | the skill's `SKILL.md` |
| token counts | `input=2, output=66, cache_read=10010` | your transcript's usage totals |
| model | `claude-opus-5` | your transcript |
| tool call counts | `Bash: 2 calls, 1 failure` | counts only, per tool name |
| session id, prompt id | opaque uuids | the harness |
| ticket id | `PLAT-42` | **parsed from your git branch name** |
| repo, team, harness | `payments-api`, `payments`, `claude-code` | git remote and configuration |
| a pseudonymous id for you | `3f9a1c7e0b2d4a86` | SHA-256 of your uid and hostname, hashed **before** it leaves the process. It answers "how many people used this skill", which the reporting floor needs; it is not reversible to your name, and the same person on two machines counts as two |
| duration | `3ms` | the harness |
| sub-agent type and id | `Explore`, `subagent-456` | the agent's catalogue name; the id is opaque |
| sub-agent depth and tool counts | `1`, `18 calls` | counts only |
| compaction reason and size | `auto`, `before=967334 after=13177` | the harness's own estimates, recorded as received |
| hook latency, by hook | `cc-status: 40ms` | the **basename** of each hook the harness timed; the path is dropped |
| session cost | `$3.20` | the harness's own running total for the session |
| a fingerprint of the skill | `a1b2c3d4e5f60718` | SHA-256 of the skill's own instructions, truncated — the file the skill ships, never anything you wrote. It exists so a skill edited without a version bump is visible rather than silently mixed into the previous version's numbers |

That is the whole list. You can print it yourself — see [verify it](#verify-it-yourself).

## What never leaves your machine

- **No prompts, no model responses, no messages.** Not truncated, not hashed — never read into a span.
- **No file contents, diffs, or paths you edited.**
- **Nothing a sub-agent was asked to do or said back.** When a sub-agent ends, the harness hands the
  hook its final reply (`last_assistant_message`) and the path to its transcript. Neither is read.
  The sub-agent's own transcript is opened only to add up token counts and tool *names*, and the task
  it was given, which sits in a file right beside it, is never opened at all.
- **No hook command lines.** A hook's latency is recorded under its basename, so `cc-status` is kept
  and the path to your dotfiles is not.
- **No commands.** Tool calls are counted by name; `tool_input` and `tool_response` are refused by
  name in `scrub()`, and a test asserts a `Bash` call reading `/etc/passwd` leaks nothing.
- **No source code, ever.**

Three independent layers enforce this. Attributes are built by naming the specific fields to keep, so
a field nobody has thought about yet — including one a future harness version adds — has no route to a
span at all. The exporter then refuses content-shaped names, and the collector drops them again on
arrival. The second and third exist because the first could have a bug; a test plants a marker string
in every documented field, one undocumented field, and a sub-agent's message bodies, and fails if it
surfaces anywhere in a span.

**One thing to be aware of:** your branch name is parsed for a ticket key and sent. If you put
something private in a branch name, it goes. Branches with no ticket key are sent as `unattributed`.

## Turning it off

```bash
export STDTEL_DISABLED=1          # this shell
```

Put it in your shell profile to make it permanent. The hook returns immediately, before reading the
payload or touching your transcript. There is no partial mode and no "anonymous" mode that still
sends: set it and nothing is emitted.

**Per skill**, an author can opt a skill out of individual attribution in its `SKILL.md`:

```yaml
metadata:
  telemetry.emit: "false"
```

That suppresses the named span for that skill. Its tokens still count toward the session total, which
is an aggregate and carries no skill name.

**Sub-agent and compaction capture** is switched off by the same `STDTEL_DISABLED=1` as everything
else. If you want the rest but not these, remove the `SubagentStart`, `SubagentStop` and `PostCompact`
blocks from your settings file; the other hooks are unaffected and nothing else changes. Note that
sub-agent cost is then read from the session directory instead, which you can see in the data as
`std.artefact.source=transcript`; removing `stdtel-hook` from the `Stop` block stops that too.

## Uninstalling

```bash
stdtel-install where              # confirm which install you are removing
uv tool uninstall stdtel          # or: pipx uninstall stdtel
```

If `stdtel-install where` finds nothing, the package was never installed — the plugin's hooks exit silently in that state, which is why you would have seen no data and no errors.

Then remove the `hooks` block from `~/.claude/settings.json` (and any project `.claude/settings.json`).
If you installed it as a plugin, `/plugin uninstall stdtel@amiable-standards`.

If `STDTEL_SPOOL=1` is set, spans are queued in `~/.stdtel/spool/` until `stdtel-export` sends them —
so working offline records rather than loses data. That directory holds the same metadata as the table
above and nothing more.

Local state lives in `~/.stdtel/sessions/` and is only ever read by the hooks on your machine:

```bash
rm -rf ~/.stdtel
```

## Verify it yourself

Do not take the table above on trust. Point it at a file instead of a collector and read what it
would have sent:

```bash
export STDTEL_OTLP_ENDPOINT=http://127.0.0.1:1   # nothing listening
echo '{"session_id":"check","cwd":"'$PWD'"}' | stdtel-hook session-start
cat ~/.stdtel/sessions/check.json                # everything it knows about your session
```

The session state file is the complete picture of what the hooks hold. If a field is not in there and
not in the table above, it is not being collected.

To see the spans themselves, run the local stack and read them back:

```bash
make up && mise run smoke
docker exec deploy-postgres-1 psql -U postgres -d stdtel -c 'SELECT * FROM skill_invocation LIMIT 1;'
```

## Seeing it in your status bar

```json
{ "statusLine": { "type": "command", "command": "stdtel-statusline" } }
```

It shows only faults you can act on — `no ticket`, `N unversioned`, `spans dropping` — and stays quiet
otherwise. **It shows no cost or token total**, by design: see the note in
[reference.md](reference.md#stdtel-statusline). `STDTEL_STATUSLINE=off` turns it off without turning
telemetry off.

## When it is not working

Telemetry failing must never block you, so hooks **always exit 0** and write errors to stderr, which
most editors do not show. A silent hook is therefore normal-looking. To check:

| symptom | check |
|---|---|
| nothing from the session you are watching | **was it already open when you installed?** Hook configuration is read at session start, so a session that predates the install runs no hooks for the rest of its life. Restart Claude Code. `stdtel-doctor` now says which project its newest data came from |
| the plugin is installed but nothing is recorded | expected when the `stdtel` package is not installed — the plugin's launcher exits silently by design. `uv tool install stdtel`, then check `stdtel-install where` |
| nothing recorded at all | `stdtel-install where` — if the hook is registered by bare name it may be unresolvable, since hooks do not get your login shell's PATH |
| skills show as `unversioned` | the catalogue cannot find them: `stdtel-validate ~/.claude/skills` |
| ticket shows `unattributed` | your branch has no ticket key, e.g. `PLAT-42-…` |
| spans stop at your machine | `curl -s localhost:8888/metrics \| grep otelcol_receiver_accepted_spans` — 0 means they never reached the collector, and the endpoint is set with `STDTEL_OTLP_ENDPOINT` (`OTEL_*` is stripped from hook subprocesses) |
| you want to see the error | run the hook by hand: `echo '{...}' \| stdtel-hook stop` |

## Questions worth asking your team

This is metadata about how you work, and reasonable people want to know how it is used. Worth
establishing before rollout, not after: who can see per-developer data, whether `std.user.hash` is
ever de-pseudonymised, and whether the data is used for individual performance assessment. Nothing in
this tool enforces those answers — they are policy, and they should be written down where you work.

The analysis this is designed for is per *skill*, not per person. The scorecard groups by skill,
version and team, and the sample-size floor means individual-level comparison is not statistically
supportable anyway — see [evaluation-power.md](evaluation-power.md).
