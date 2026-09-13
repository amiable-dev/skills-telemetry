# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

- **Preferred**: [report it privately](https://github.com/amiable-dev/skills-telemetry/security/advisories/new)
  through GitHub. The report is visible only to maintainers.
- **Email**: security@amiable.dev.

Please include what you observed, how to reproduce it, which version, and what you think the impact
is. A suggested fix is welcome but not expected.

You will get an acknowledgement within 48 hours and a substantive response within 7 days. We will
credit you in the advisory unless you would rather stay anonymous, and we ask for reasonable time to
ship a fix before public disclosure.

## Supported versions

| version | supported |
|---|---|
| 0.2.x | ✅ |
| < 0.2 | ❌ |

Fixes ship as a patch on the latest minor. Nothing is backported below it; upgrade instead.

While the project is pre-1.0 the supported range is deliberately narrow. `uv tool upgrade stdtel`
is the whole upgrade path.

## What this tool collects

This is telemetry software that runs inside your coding sessions, so the honest answer to "is it safe
to install" is a list, not an assurance. [docs/for-developers.md](docs/for-developers.md) enumerates
every field that leaves the machine. In summary:

- **Metadata only.** Skill names and versions, token counts, durations, tool-call counts and failure
  counts, the ticket key parsed from your git branch, and the repo name.
- **Never** prompt text, model responses, file contents, file paths, diffs, or tool arguments. The
  hook never puts them in a span; `stdtel.exporter.scrub()` drops any content-prefixed attribute
  before anything is exported or written to the spool, even if handed one; and the collector's
  `attributes/drop_content` processor deletes them again on arrival. Three layers, because any one of
  them is a configuration change away from being wrong.
- **Nothing leaves by default.** Spans go to the OTLP endpoint *you* configure (`STDTEL_OTLP_ENDPOINT`).
  There is no vendor endpoint, no default upload target, and no phone-home.
- **`STDTEL_DISABLED=1`** turns it off entirely, checked before the payload is read — an opt-out that
  still parsed your transcript would not be one.

If you find a path by which content does escape, that is a vulnerability in this project's core
promise. Please report it privately.

## Running the local stack

`mise run up` brings up a **development** stack: Grafana with anonymous Admin access, and Postgres,
ClickHouse and MinIO on well-known passwords that are committed to this repository on purpose.

It binds to `127.0.0.1` by default for exactly that reason. `STDTEL_BIND=0.0.0.0 mise run up` exposes
it to your network; do not do that outside a trusted one, and do not treat this stack as a template
for a deployed one. It is not hardened and is not meant to be.

## Hook execution

The hooks run as your user, inside your shell, on every tool call. Two consequences worth knowing:

- They always exit 0. A telemetry failure must never block a developer, so a crash is printed to
  stderr and swallowed. That is deliberate (ADR-003) and it means a broken install is quiet — run
  `stdtel-doctor`, which reports it explicitly.
- `stdtel-install` writes an **absolute path** into your settings, because a hook gets a non-login
  `sh -c`. Check what it wrote with `stdtel-install where` if you want to see exactly what will run.
