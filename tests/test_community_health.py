"""The repository's own contract with people who are not us.

These files are the difference between public and open source. They are checked
here rather than trusted because each one makes a claim that can quietly stop
being true: a licence the package does not carry, a security policy pointing at
a channel that is switched off, a stack that says loopback and publishes to the
network.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_licence_exists_and_matches_the_metadata():
    """`license = "MIT"` with no LICENSE file leaves the default: all rights reserved."""
    licence = (ROOT / "LICENSE").read_text()
    assert "MIT License" in licence
    assert "WITHOUT WARRANTY OF ANY KIND" in licence
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["license"] == "MIT"
    assert "LICENSE" in pyproject["project"]["license-files"], \
        "the licence must ship in the sdist and wheel, not only sit in the repo"


def test_community_health_files_are_present():
    for name in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md",
                 "CITATION.cff", ".github/PULL_REQUEST_TEMPLATE.md", ".github/CODEOWNERS",
                 ".github/dependabot.yml", ".github/ISSUE_TEMPLATE/config.yml",
                 ".github/ISSUE_TEMPLATE/bug_report.md",
                 ".github/ISSUE_TEMPLATE/feature_request.md"):
        assert (ROOT / name).is_file(), f"{name} is missing"


def test_codeowners_names_no_team():
    """A personal account has no teams, and GitHub ignores an owner it cannot resolve.

    `@owner/maintainers` looks like a review requirement and enforces nothing.
    """
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        for owner in re.findall(r"@[\w/-]+", line):
            assert "/" not in owner, f"{owner} is a team; this account has none"


def test_security_policy_says_what_is_collected():
    """The question a reviewer actually asks about telemetry software."""
    policy = (ROOT / "SECURITY.md").read_text()
    assert "STDTEL_DISABLED" in policy, "the opt-out must be in the security policy"
    assert "security/advisories/new" in policy, "no private reporting route"
    for claim in ("prompt text", "file contents"):
        assert claim in policy


def test_the_local_stack_binds_to_loopback_by_default():
    """Grafana runs anonymous-Admin; on 0.0.0.0 that is an open dashboard.

    Checked as a property of every published port, so a service added later
    cannot quietly reintroduce the exposure.
    """
    compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
    published = re.findall(r'"([^"]*\d+:\d+)"', compose)
    assert published, "no published ports found - has the compose file moved?"
    for entry in published:
        assert entry.startswith("${STDTEL_BIND:-127.0.0.1}:"), \
            f"{entry} publishes without the loopback default"


def test_gitleaks_allowlist_is_scoped_to_the_development_stack():
    """An allowlist wide enough to be convenient stops being a scanner."""
    config = (ROOT / ".gitleaks.toml").read_text()
    assert "useDefault = true" in config
    paths = re.findall(r"'''(.+?)'''", config)
    assert paths, "no allowlist entries parsed"
    for path in paths:
        assert path.startswith(("deploy/", "docs/")) or path.startswith("toolu_"), \
            f"{path} allowlists more than the local stack"
