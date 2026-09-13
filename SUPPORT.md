# Support

## Start here

`stdtel-doctor`. It runs six checks and every failure names its remedy. Most problems people hit are
one of them, and the tool is deliberately quiet otherwise — hooks always exit 0, so a broken install
produces no data and no error. `stdtel-doctor` is how you find that out.

| question | where |
|---|---|
| what is actually collected, and how do I turn it off | [docs/for-developers.md](docs/for-developers.md) |
| every CLI, and the authoritative environment-variable table | [docs/reference.md](docs/reference.md) |
| bringing up the local stack, and what each endpoint is | [docs/local-stack.md](docs/local-stack.md) |
| which skill or agent to use for what | [docs/skills.md](docs/skills.md) |
| how much data before the numbers mean anything | [docs/evaluation-power.md](docs/evaluation-power.md) |
| worked examples, and the misreadings this data invites | [docs/insight-walkthroughs.md](docs/insight-walkthroughs.md) |
| why something is built the way it is | [docs/adrs/](docs/adrs/) |

## Where to ask

- **Questions, ideas, "is this supposed to happen"** —
  [Discussions](https://github.com/amiable-dev/skills-telemetry/discussions).
- **Bugs and feature requests** —
  [Issues](https://github.com/amiable-dev/skills-telemetry/issues). Please include the output of
  `stdtel-doctor`, your harness and version, and your OS.
- **Security vulnerabilities** — privately, never in an issue. See [SECURITY.md](SECURITY.md).

## Empty dashboards

The single most common report, and it has one root cause with several faces: something in the chain
succeeded while doing nothing.

1. `stdtel-doctor` — does it say the hook is resolvable and the package installed? The plugin's
   launcher exits 0 in silence when it cannot find the CLI, by design, so an uninstalled package
   looks exactly like a working one.
2. Is there a file in `~/.stdtel/sessions/`? If not, the hooks are not firing.
3. Is your branch ticket-prefixed? Unattributed sessions are kept for cost analysis and **excluded**
   from outcome analysis, so they will not appear in the scorecard.
4. Below the floor — 30 merged PRs per arm — the scorecard returns `insufficient-data` on purpose.
   That is the correct answer, not a fault. `mise run demo` loads a synthetic fleet if you want to
   see what the dashboards look like with enough data behind them.

## What this project does not promise

It is pre-1.0 and the data model still moves. There is no support contract, no SLA, and no
commitment to a stable schema between minor versions. Read the [CHANGELOG](CHANGELOG.md) before
upgrading.
