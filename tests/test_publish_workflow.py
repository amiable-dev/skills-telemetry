"""Guards on the publishing workflow.

PyPI never allows re-uploading a version, even after deletion, so a bad publish
burns the number permanently. These are the properties that keep that from
happening by accident.
"""
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
