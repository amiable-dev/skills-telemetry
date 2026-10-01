---
name: stdtel-setup
description: Set up standards telemetry with the user, for Claude Code and GitHub Copilot — installs the hooks, switches on each harness's own telemetry without content, and offers Claude Code's detailed view (OTEL_LOG_TOOL_DETAILS) only with the user's informed consent and after the collector has proven it drops content. Use when installing or re-installing stdtel, when the user asks to enable Copilot telemetry, or when third-party skills show up as "third-party" in cost data.
license: MIT
metadata:
  version: "1.1.0"
  standard_id: STD-TEL-001
  policy_ids: "telemetry.manifest_valid"
  owner: platform-observability
  harness_support: "claude-code, copilot-vscode, copilot-cli"
  telemetry.emit: "true"
  telemetry.success_signal: manual
---

# Set up standards telemetry

You are configuring telemetry on the user's machine **with** them. Every step that changes a file is
shown first and done only when they agree. You decide nothing on their behalf: your job is to make each
choice informed, and checkable afterwards. ([ADR-014](../../docs/adrs/014-harness-native-telemetry.md)
decision 12.)

## 1. Find out what is here

```bash
stdtel-install where            # is the package installed? (exit 1: `uv tool install stdtel` first)
command -v claude code copilot  # which harnesses exist
stdtel-doctor --quiet           # anything already broken
```

Ask which harnesses they use. Do not configure one they do not use.

Find out where the collector is. `stdtel-install settings --dry-run` shows the endpoint in the
`OTEL_EXPORTER_OTLP_ENDPOINT` line, taken from `STDTEL_OTLP_ENDPOINT`. If nothing answers there, the
local stack is `make up` in the skills-telemetry repository. Do not start a second one if a shared one
exists.

## 2. Claude Code

First, find out who registers the hooks. If `stdtel-doctor` reports them as coming from the plugin, or
`~/.claude/settings.json` lists `stdtel@...` under `enabledPlugins`, **the plugin already registers
them**. Writing them into settings as well would make every hook fire twice. In that case use
`--native-only`:

```bash
stdtel-install settings --native-only --dry-run   # plugin install: env only
stdtel-install settings --dry-run                 # package-only install: hooks and env
```

Show them the dry run, then run the same command without `--dry-run` on their yes.

Without `--native-only`, this registers the hooks and switches on Claude Code's own telemetry. That means one event per model
request, with exact cost and the skill, agent, plugin and MCP server it served. Tell them in one
sentence what it does **not** do: no prompts, no responses and no tool inputs are sent. Their email and
account ids are sent, and the collector deletes them on arrival.

`--no-native` installs the hooks alone, if that is what they want.

## 3. Copilot

```bash
stdtel-install copilot                                   # prints both blocks below
stdtel-install copilot --vscode-settings "<their settings.json>"   # or merge, on their yes
```

- **VS Code:** it writes `github.copilot.chat.otel.*` with `captureContent: false`. If their settings
  file has comments, the command refuses to rewrite it. In that case, show them the block to paste.
  The user settings file is `~/Library/Application Support/Code/User/settings.json` on macOS,
  `~/.config/Code/User/settings.json` on Linux, and `%APPDATA%\Code\User\settings.json` on Windows.
- **Copilot CLI:** it prints `export COPILOT_OTEL_*` lines for their shell profile. Do not edit their
  profile for them unless they ask.
- **In an organisation:** the enterprise-managed `telemetry` policy is the better route, because it
  applies to everyone at once. Explain it and point them to their admin. Do not try to set it.

**Never** enable Copilot's `captureContent`. It has no collector-side gate here, and Copilot has sent
content even with it off (microsoft/vscode#326254). That is why the collector deletes it regardless.

## 4. Offer the detailed view — Claude Code only

Recommend `OTEL_LOG_TOOL_DETAILS=1`, and say plainly what it costs and what it buys.

**What it costs.** Claude Code will send tool inputs to the collector:
- full shell commands;
- file paths;
- **the contents of files written or edited**;
- MCP tool arguments.

The collector deletes all of these before anything is stored. But they do travel as far as the
collector, and if the collector is not on this machine, across the network to it.

**What it buys.**
- Skills from third-party plugins are named in cost data. Without the flag they arrive as
  `"third-party"`, and a request in a prompt that ran more than one such skill cannot be attributed.
- Commits made in a session are recorded with their SHA.

Then **ask, and wait for an explicit yes.** Silence, "sure, whatever you think" or a yes to something
else is not consent. Ask again or leave it off.

On a clear yes:

```bash
stdtel-install detailed-view on
```

That command enforces the rest itself. It refuses unless:
- the collector is on this machine, **or** the user has told you it applies the same privacy rules.
  Only then add `--collector-confirmed`. Never add it on your own judgement;
- `stdtel-doctor --content-check` passes **now**. It sends a marker in content-shaped fields and checks
  that the marker is absent from Tempo and Loki, while a control id sent alongside it is present.

If it refuses, tell them why in its own words and leave the flag off. Do not work around it, and do
not set `OTEL_LOG_TOOL_DETAILS` by hand.

To turn it off later: `stdtel-install detailed-view off`.

## 5. Finish

1. **Restart.** Each harness reads its configuration at startup, so a session that is already open,
   including this one, records nothing new.
2. After the restart, run `stdtel-doctor`. It re-runs the content check whenever the detailed view is
   on.
3. Report what was configured, what was left off and why, and the one command that undoes each part.
4. For what is collected, and every way to switch it off, point them to
   [docs/for-developers.md](../../docs/for-developers.md). `STDTEL_DISABLED=1` stops stdtel's hooks,
   **not** the harnesses' own telemetry.
