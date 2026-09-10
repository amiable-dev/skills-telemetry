"""Every user-facing entry point must be documented.

New scripts and skills ship undocumented unless something fails. These tests are
that something: they read pyproject and the skills tree rather than a hand-kept
list, so adding an entry point without documenting it breaks the build.
"""
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = (ROOT / "docs" / "reference.md").read_text()
SKILLS_DOC = (ROOT / "docs" / "skills.md").read_text()
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
SCRIPTS = sorted(PYPROJECT["project"]["scripts"])


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_console_script_is_documented(script):
    assert f"## `{script}`" in REFERENCE, f"{script} has no reference entry"


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_documented_script_lists_its_exit_codes_or_says_why_not(script):
    section = REFERENCE.split(f"## `{script}`")[1].split("\n## ")[0]
    assert "xit" in section, f"{script}: document exit behaviour"


def test_every_skill_has_a_usage_entry():
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        name = skill.parent.name
        assert f"skills/{name}`" in SKILLS_DOC, f"skill {name} is undocumented"


def test_every_agent_has_a_usage_entry():
    for agent in sorted((ROOT / "agents").glob("*.md")):
        assert f"agents/{agent.stem}`" in SKILLS_DOC, f"agent {agent.stem} is undocumented"


def test_each_skill_entry_says_when_not_to_use_it():
    """"When to use" alone sends people to the wrong tool confidently."""
    for heading in re.findall(r"^## `(skills/[\w-]+|agents/[\w-]+)`", SKILLS_DOC, re.M):
        section = SKILLS_DOC.split(f"## `{heading}`")[1].split("\n## ")[0]
        assert "Do not use it" in section or "Note" in section, f"{heading}: no counter-indication"


# --- the environment variable table is authoritative ---

def test_every_env_var_read_by_the_code_is_documented():
    used = set()
    for py in list((ROOT / "stdtel").rglob("*.py")) + list((ROOT / "eval").rglob("*.py")):
        used |= set(re.findall(r'environ\.get\(\s*"(STDTEL_[A-Z_]+|OTEL_[A-Z_]+|CLAUDE_[A-Z_]+)"', py.read_text()))
    table = REFERENCE.split("## Environment variables")[1]
    missing = {v for v in used if f"`{v}`" not in table}
    assert not missing, f"undocumented environment variables: {sorted(missing)}"


def test_env_table_does_not_invent_variables():
    """A documented variable nothing reads is a lie with a long half-life."""
    source = "\n".join(p.read_text() for p in
                       list((ROOT / "stdtel").rglob("*.py")) + list((ROOT / "eval").rglob("*.py")))
    table = REFERENCE.split("## Environment variables")[1]
    documented = set(re.findall(r"\| `(STDTEL_[A-Z_]+)`", table))
    for var in documented:
        assert var in source, f"{var} is documented but never read"


def test_dry_run_semantics_are_called_out():
    """--dry-run returns True for every policy; read as a result it is a clean sweep."""
    section = REFERENCE.split("## `stdtel-eval`")[1].split("\n## ")[0]
    assert "does not evaluate anything" in section
