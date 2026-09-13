# Releasing

Publishing uses **PyPI Trusted Publishing** (OIDC), so no API token is stored in this repository.
That requires a one-time setup on PyPI itself, which cannot be automated from here.

## One-time: register the publisher (you must do this)

PyPI needs to be told which workflow, in which repository, is allowed to publish. Do this **before**
the first release, on both indexes.

1. **Test PyPI** — https://test.pypi.org/manage/account/publishing/
2. **PyPI** — https://pypi.org/manage/account/publishing/

Under *"Add a new pending publisher"*, enter exactly:

| field | value |
|---|---|
| PyPI Project Name | `stdtel` |
| Owner | `amiable-dev` |
| Repository name | `skills-telemetry` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` (on PyPI) / `testpypi` (on Test PyPI) |

The environment name must match, or the publish is rejected with a mismatched-claim error.

> Use a **pending** publisher while the project does not exist yet on that index. PyPI creates the
> project on first successful publish. The name `stdtel` is unregistered at the time of writing;
> if someone takes it first, the name in `pyproject.toml` and all three plugin manifests must change
> together — a test enforces that they agree.

Then create the matching GitHub environments — Settings → Environments → `pypi` and `testpypi`. They
can be empty; their existence is what the workflow's `environment:` key binds to. Adding a required
reviewer to `pypi` gives you a human gate on every publish, which is worth having.

## Every release

```bash
# 1. bump the version in all four places (a test asserts they agree)
#    pyproject.toml · plugin.json · .claude-plugin/plugin.json · .claude-plugin/marketplace.json
# 2. update CHANGELOG.md
# 3. merge to main, then:
gh release create v0.3.0 --title "0.3.0" --notes-file <(sed -n '/## 0.3.0/,/^## /p' CHANGELOG.md)
```

The workflow then builds, checks the tag matches the version, **installs the wheel into a clean venv
and runs every console script**, and publishes. A wheel that cannot be installed fails on someone
else's machine rather than ours, so that check is not optional.

## Rehearse first

```bash
gh workflow run publish.yml -f target=testpypi
uv tool install --index https://test.pypi.org/simple/ stdtel   # verify it installs from the index
```

Test PyPI is the only way to find out that a package is broken *before* the version number is burned:
PyPI does not allow re-uploading a version, even after deletion.

## Why the version appears in four files

The Python package and the plugin ship together, and the marketplace serves a cached copy until the
version changes. A mismatch means someone installs the plugin and gets code from a different release.
`tests/test_distribution.py::test_versions_agree_across_manifests` fails if they drift.
