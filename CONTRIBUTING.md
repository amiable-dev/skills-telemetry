# Contributing

Thanks for looking. This project measures whether coding-agent skills actually help, so the bar for
its own claims is high: a change that reports success without doing anything is the specific failure
mode this repo exists to catch, and several of the tests exist because a component did exactly that.

## Setup

[mise](https://mise.jdx.dev) owns the toolchain. It pins Python 3.13 and auto-activates `.venv`.

```bash
git clone https://github.com/amiable-dev/skills-telemetry
cd skills-telemetry
mise install
mise run ci        # validate + test + eval-dry + power-check
```

`mise run ci` is the whole gate. If it passes locally it passes in CI, with one exception: the
policy tests need [OPA](https://www.openpolicyagent.org/docs/latest/#running-opa) on your PATH. They
skip without it locally, and CI fails if they skip — the primary metric going untested while the run
reports green is not an outcome we accept.

Tasks: `mise tasks`. The ones you will want are `test`, `validate`, `up` (local stack),
`smoke` (verify every hop of it), `demo` (synthetic data so dashboards have something to show).

## How to work here

**Write the test first.** Red, then green. Several bugs in this repo were pinned in place by a test
that asserted the broken behaviour, so a test that passes before your fix is evidence of nothing.

**Assert on properties, not shapes.** `tests/test_publish_workflow.py` asserts "the real index is
unreachable by manual dispatch", not "line 34 says `pypi`". The first survives a refactor; the second
fails on formatting and passes on regressions.

**Never record an unobserved value** ([ADR-005](docs/adrs/005-data-integrity.md)). If a component
cannot do its job it must fail loudly. Vacuous truth is a bug: a policy that passes because it
matched nothing, a check that reports "0 valid" for a missing directory, a suite that skips.

**Metadata only.** No prompt text, response text, file content, path or tool argument may ever reach
a span. See [SECURITY.md](SECURITY.md) — this is the project's core promise and PRs that weaken it
will not be merged.

**Branch names carry the ticket key** — `feature/STDTEL-42-short-description`. The telemetry joins on
exactly this: `std.ticket.id` is parsed from the branch, and work on an unprefixed branch is recorded
as `unattributed` and excluded from outcome analysis. This repo dogfoods its own tool, so the
convention is load-bearing, not cosmetic.

## Decisions

Anything that changes an interface, a data contract, or something a future contributor would
otherwise re-litigate goes in an ADR under [`docs/adrs/`](docs/adrs/). Record what was **rejected**
and what the decision **costs** — that is the part that stops the argument happening twice.
`tests/test_adrs.py` checks the index stays in step.

## Things the tests will catch

Worth knowing before you are surprised by them:

- **Adding a console script or a skill without documenting it fails `tests/test_docs_coverage.py`.**
- **The version appears in five files** — `pyproject.toml`, `plugin.json`,
  `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `CITATION.cff` — and a test
  asserts they agree. Bump all five for anything a user receives; the marketplaces serve a cached
  copy until the number changes.
- **Reporting thresholds are quoted verbatim in four places.** `eval.power.HARD_FLOOR` is canonical;
  a test asserts the sentence appears identically in the README, the analyst agent and the query
  skill. Do not reword it in one of them.
- **`docs/evaluation-power.md` is generated.** Edit `eval/power.py` and run `mise run power`;
  `--check` fails on drift.
- **`collector/copilot-skill-map.yaml` is generated** by `mise run skill-map`.
- **`SessionState.save()` must list every field.** Each hook is a separate process, so a field you
  forget to persist reads as its default at the next event — silently.

## Pull requests

- One concern per PR, with the reasoning in the description. What broke, why this fix, what it costs.
- Update `CHANGELOG.md` for anything user-facing.
- Say what you verified and how. "Tests pass" is weaker than "reproduced the failure, fixed it, the
  regression test fails without the fix".
- CI must be green. `policy-results` also runs on PRs: it grades the tree with OPA and uploads the
  artefact the primary metric is computed from.

## Reporting things

- Bugs and features: [issues](https://github.com/amiable-dev/skills-telemetry/issues), using the
  templates.
- Questions: [discussions](https://github.com/amiable-dev/skills-telemetry/discussions).
- Security: **privately**, see [SECURITY.md](SECURITY.md).

By contributing you agree your work is licensed under the [MIT License](LICENSE).
