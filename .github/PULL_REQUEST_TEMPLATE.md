## What this changes

One concern per PR. What was wrong, and why this fix rather than another.

## Why

What it costs, and what you rejected. If it changes an interface or a data contract, it needs an ADR
under `docs/adrs/` — that is the part that stops the argument happening twice.

## How you verified it

Not "tests pass". What did you reproduce, and how do you know the fix works?

- [ ] The test fails without the fix (red, then green)
- [ ] `mise run ci` is green locally
- [ ] Policy tests did not skip — OPA is on my PATH

## Checklist

- [ ] `CHANGELOG.md` updated, if this is user-facing
- [ ] All four version files bumped together, if this ships to users
- [ ] Docs updated — a new console script or skill fails `tests/test_docs_coverage.py` without it
- [ ] No prompt, response, file content, path or tool argument can reach a span
