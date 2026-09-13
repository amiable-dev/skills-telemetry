# What this collects, and how to switch it off

This runs on your machine, in your editor, on every tool call. You are entitled to know exactly what
leaves it and to stop it without asking anyone. This page is that.

If you only read one thing: **`export STDTEL_DISABLED=1` turns it off completely**, and it takes
effect before your transcript is even opened.

## What leaves your machine

One span per skill invocation and one per turn, containing only these fields:

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
| duration | `3ms` | the harness |

That is the whole list. You can print it yourself — see [verify it](#verify-it-yourself).

## What never leaves your machine

- **No prompts, no model responses, no messages.** Not truncated, not hashed — never read into a span.
- **No file contents, diffs, or paths you edited.**
- **No commands.** Tool calls are counted by name; `tool_input` and `tool_response` are refused by
  name in `scrub()`, and a test asserts a `Bash` call reading `/etc/passwd` leaks nothing.
- **No source code, ever.**

Two independent layers enforce this: the exporter refuses content-shaped attribute names before
sending, and the collector drops them again on arrival. The second exists because the first could have
a bug.

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
