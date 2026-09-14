"""Guards on the publishing workflow.

PyPI never allows re-uploading a version, even after deletion, so a bad publish
burns the number permanently. These are the properties that keep that from
happening by accident.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "publish.yml").read_text())
# PyYAML reads the `on:` key as the boolean True
TRIGGERS = WF.get("on") or WF.get(True)
JOBS = WF["jobs"]


def test_real_pypi_can_only_be_reached_by_a_release():
    """A manual dispatch skips the version-vs-tag check, so it must not be able
    to publish to the real index. It could push an arbitrary build under any
    version number, and that number can never be reused."""
    condition = JOBS["pypi"]["if"]
    assert "workflow_dispatch" not in condition, condition
    assert "release" in condition


def test_manual_dispatch_targets_only_test_pypi():
    options = TRIGGERS["workflow_dispatch"]["inputs"]["target"]["options"]
    assert options == ["testpypi"], f"manual publishing to {options} is not gated by a tag"


def test_the_version_is_checked_against_the_tag():
    step = next(s for s in JOBS["build"]["steps"] if "tag" in s.get("name", "").lower())
    assert "release" in step["if"]


def test_the_wheel_is_exercised_before_anything_is_published():
    """Publishing something that cannot be installed fails on someone else's
    machine, not ours."""
    names = [s.get("name", "") for s in JOBS["build"]["steps"]]
    assert any("install and run" in n for n in names), names
    for job in ("pypi", "testpypi"):
        assert JOBS[job]["needs"] == "build" or "build" in JOBS[job]["needs"]


def test_publishing_jobs_use_an_environment():
    """The environment is what the OIDC claim binds to, and where a human gate
    can be attached."""
    assert JOBS["pypi"]["environment"] == "pypi"
    assert JOBS["testpypi"]["environment"] == "testpypi"


def test_no_long_lived_token_is_referenced():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    for banned in ("PYPI_API_TOKEN", "password:", "secrets.PYPI"):
        assert banned not in text, f"trusted publishing means no {banned}"


def test_id_token_is_granted_only_where_it_is_needed():
    assert WF.get("permissions", {}).get("id-token") != "write", \
        "id-token: write belongs on the publishing jobs, not the whole workflow"
    for job in ("pypi", "testpypi"):
        assert JOBS[job]["permissions"]["id-token"] == "write"


# --- #34: the publish job holds id-token: write, so what it runs must be fixed ---

def _uses(workflow: dict) -> list[str]:
    return [step["uses"] for job in workflow.get("jobs", {}).values()
            for step in job.get("steps", []) if "uses" in step]


def _all_workflows():
    import yaml
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        yield path, yaml.safe_load(path.read_text())


def test_every_action_is_pinned_to_a_commit():
    """A tag is a pointer somebody else can move.

    `pypa/gh-action-pypi-publish@release/v1` runs in the job that holds
    `id-token: write`, so whoever controls that tag controls what gets published
    as `stdtel` — permanently, since PyPI never allows re-uploading a version.
    """
    for path, workflow in _all_workflows():
        for ref in _uses(workflow):
            _, _, version = ref.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", version), \
                f"{path.name}: {ref} is a mutable reference"


def test_each_pin_records_the_version_it_came_from():
    """A bare SHA is unreadable and un-reviewable; the comment is what makes a
    Dependabot bump legible in a diff."""
    for path, _ in _all_workflows():
        for line in path.read_text().splitlines():
            if "uses:" in line and "@" in line:
                assert "#" in line.split("@", 1)[1], f"{path.name}: {line.strip()} has no version comment"


def test_the_published_artefact_is_verified_after_it_lands():
    """Everything else proves the wheel we built works, not the one on the index."""
    jobs = WF["jobs"]
    assert "verify" in jobs and jobs["verify"]["needs"] == "pypi"
    body = json.dumps(jobs["verify"])
    assert "stdtel==" in body, "it must install the exact published version"
    assert "attestation verify" in body


def test_verification_does_not_get_the_publishing_credential():
    """Reading an attestation does not require minting one."""
    assert "id-token" not in (WF["jobs"]["verify"].get("permissions") or {})


def test_a_release_must_be_described_in_the_changelog():
    steps = json.dumps(WF["jobs"]["build"]["steps"])
    assert "CHANGELOG.md" in steps


def test_the_sdist_does_not_ship_an_unrunnable_test_suite():
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "prune tests" in manifest
