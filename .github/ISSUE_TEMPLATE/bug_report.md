---
name: Bug report
about: Something behaves differently from what it claims
title: ""
labels: bug
---

## What happened

What you observed. If a component reported success while doing nothing, say so explicitly — that is a
whole class of bug here and it is easy to describe as "nothing happened".

## What you expected

## Steps to reproduce

1.
2.
3.

## `stdtel-doctor`

```
paste the full output
```

It names the remedy for most problems, and its output tells us more than any other single thing.

## Environment

- stdtel version: <!-- stdtel-validate --help, or `uv tool list` -->
- installed how: <!-- uv tool install / pipx / from a checkout / plugin -->
- harness and version: <!-- Claude Code 2.x / VS Code Copilot -->
- OS:
- Python:

## Logs

```
Hooks always exit 0, so failures go to stderr and are swallowed. Anything printed by
`stdtel:` in your session, and `~/.stdtel/sessions/<id>.json` if it exists, helps.
Please remove anything you do not want public — though note this tool is not supposed
to produce content in the first place, and if it did, that is the bug.
```

## Anything else
