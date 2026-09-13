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

Then create the matching GitHub environments — Settings → Environments → `pypi` and `testpypi`. Their
existence is what the workflow's `environment:` key binds to.

`pypi` needs one setting beyond existing: under **Deployment branches and tags**, add a rule for the
tag pattern `v*`. A release fires the workflow on a *tag* ref, and the default "Protected branches
only" policy rejects tag refs — the run reaches the `pypi` job and stops there with a branch-policy
error, after the build has already passed. Adding a **required reviewer** to `pypi` on top of that
gives you a human gate on every publish, which is worth having.

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

## Safety properties

These are enforced by `tests/test_publish_workflow.py`, because the cost of getting them wrong is
permanent — PyPI never allows re-uploading a version, even after deletion.

- **The real index is reachable only by publishing a release.** A manual dispatch has no tag, so the
  version-vs-tag check cannot run; allowing manual publishes to PyPI would let any build go out under
  any version number. Manual dispatch targets Test PyPI only.
- **The version must not already exist** on the target index. Checked before the build is uploaded, so
  the failure names the version rather than surfacing as an opaque 400 at the last step.
- **The wheel is installed into a clean venv and every console script is run** before anything
  publishes.
- **No long-lived token** exists in the repository; `id-token: write` is granted to the publishing jobs
  only, not the whole workflow.

One more is worth adding on the GitHub side, which cannot be asserted from here: a **required
reviewer** on the `pypi` environment. Its deployment rule is already restricted to `v*` tags.

## Rehearse first

```bash
gh workflow run publish.yml -f target=testpypi
# Test PyPI carries stdtel but not its dependencies, so the resolver must be allowed
# to fall back to the real index for those:
uv tool install stdtel --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match
```

Those switches get recorded in the tool's `uv-receipt.toml`, so a later `uv tool upgrade` keeps
resolving from Test PyPI. Reinstall cleanly (`uv tool uninstall stdtel && uv tool install stdtel`)
once the real release is out, rather than leaving a rehearsal install as the working setup.

Test PyPI is the only way to find out that a package is broken *before* the version number is burned:
PyPI does not allow re-uploading a version, even after deletion.

## Why the version appears in four files

The Python package and the plugin ship together, and the marketplace serves a cached copy until the
version changes. A mismatch means someone installs the plugin and gets code from a different release.
`tests/test_distribution.py::test_versions_agree_across_manifests` fails if they drift.
