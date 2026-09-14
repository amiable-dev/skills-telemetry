import os

import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test runs against a throwaway state dir and HOME.

    HOME matters because the hooks fall back to ~/.claude/skills: without this
    the developer's own catalogue would leak into assertions about versions.
    """
    # Clear every STDTEL_* first, then set what the suite wants. A list of
    # variables to neutralise goes stale the moment a new one is added: once
    # STDTEL_SKILLS_OVERLAY existed, a developer who had configured one saw two
    # unrelated tests fail, because the real overlay leaked into assertions about
    # an empty catalogue. Enumerating the environment cannot go stale.
    for var in [v for v in os.environ if v.startswith("STDTEL_")]:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", "skills")
    monkeypatch.setenv("STDTEL_BRANCH", "feature/PLAT-123-structured-logging")
    monkeypatch.setenv("STDTEL_REPO", "git@github.com:org/payments-api.git")
    monkeypatch.setenv("STDTEL_TEAM", "payments")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    yield home
