"""Root resolution for the skill catalogue used by the hooks (STDTEL_SKILLS_ROOT)."""
import os
from pathlib import Path

import pytest

from stdtel.hooks.cli import _catalogue, skills_roots
from tests.test_manifest import GOOD


def write_skill(root: Path, name: str, version: str = "1.0.0") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(GOOD.replace("name: x", f"name: {name}").replace("1.0.0", version))
    return d


@pytest.fixture
def home(isolated_state, monkeypatch):
    """The isolated HOME from conftest, with no skills root configured."""
    monkeypatch.delenv("STDTEL_SKILLS_ROOT", raising=False)
    return isolated_state


def test_falls_back_to_user_skills_dir(home):
    write_skill(home / ".claude" / "skills", "user-skill", "3.1.0")
    assert skills_roots() == [home / ".claude" / "skills"]
    assert _catalogue()["user-skill"].version == "3.1.0"


def test_missing_roots_are_dropped(home, monkeypatch):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(home / "nope"))
    assert skills_roots() == []          # user dir does not exist either
    assert _catalogue() == {}


def test_relative_root_resolves_against_project_dir(home, monkeypatch):
    project = home / "projects" / "payments-api"
    write_skill(project / "skills", "project-skill", "4.0.0")
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", "skills")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.chdir(home)              # cwd deliberately elsewhere
    assert skills_roots() == [project / "skills"]
    assert _catalogue()["project-skill"].version == "4.0.0"


def test_user_dir_is_searched_last(home, monkeypatch):
    project = home / "project"
    write_skill(project / "skills", "overridden", "9.0.0")
    write_skill(home / ".claude" / "skills", "overridden", "1.0.0")
    write_skill(home / ".claude" / "skills", "user-only", "2.0.0")
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(project / "skills"))
    cat = _catalogue()
    assert cat["overridden"].version == "9.0.0"   # project catalogue wins
    assert cat["user-only"].version == "2.0.0"    # user catalogue still merged


def test_multiple_roots_first_wins(home, monkeypatch):
    project, shared = home / "project", home / "shared"
    write_skill(project, "overridden", "9.0.0")
    write_skill(shared, "overridden", "1.0.0")
    write_skill(shared, "shared-only", "2.0.0")
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", os.pathsep.join([str(project), str(shared)]))
    cat = _catalogue()
    assert cat["overridden"].version == "9.0.0"
    assert cat["shared-only"].version == "2.0.0"


def test_tilde_is_expanded_and_duplicates_collapse(home, monkeypatch):
    write_skill(home / "elsewhere", "tilde-skill")
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", os.pathsep.join(["~/elsewhere", str(home / "elsewhere")]))
    assert skills_roots() == [home / "elsewhere"]
