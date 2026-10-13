"""ADR-015 decision 6 (#155): instructions are the last layer, and they point at the mechanisms.

String checks only (ADR-007): they prove each surface says the right thing, not
that a model follows it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "skills" / "stdtel-setup" / "SKILL.md"
QUERY = ROOT / "skills" / "stdtel-query" / "SKILL.md"
ANALYST = ROOT / "agents" / "skill-scorecard-analyst.md"
CLAUDE_MD = ROOT / "CLAUDE.md"


def _section(text: str, heading: str) -> str:
    """The body under a `## heading` up to the next `## `."""
    m = re.search(rf"^## (?:\d+\. )?{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, heading
    return m.group(1)


@pytest.mark.parametrize("path", [SETUP, QUERY, ANALYST, CLAUDE_MD], ids=lambda p: p.name)
def test_each_surface_names_doctor_json(path):
    assert "stdtel-doctor --json" in path.read_text()


def test_setup_installs_the_scheduled_loader():
    text = SETUP.read_text()
    assert "stdtel-install loader" in text
    assert "stdtel-install loader --uninstall" in text, "and the one command that undoes it"


def test_setup_ends_by_running_doctor():
    finish = _section(SETUP.read_text(), "Finish")
    assert "stdtel-doctor --json" in finish
    assert finish.find("stdtel-doctor --json") > finish.find("Restart") >= 0, "after the restart"
    assert _section(SETUP.read_text(), "Finish") is not None
    assert SETUP.read_text().rstrip().endswith(finish.rstrip()), "Finish is the last section"


@pytest.mark.parametrize("path", [QUERY, ANALYST], ids=lambda p: p.name)
def test_doctor_runs_before_any_warehouse_number(path):
    text = path.read_text()
    doctor = text.find("stdtel-doctor --json")
    first_store = min(i for i in (text.find("psql"), text.find("```sql"), text.find("**Postgres**")) if i >= 0)
    assert 0 <= doctor < first_store, "doctor comes first"


@pytest.mark.parametrize("path", [QUERY, ANALYST], ids=lambda p: p.name)
def test_a_non_pass_freshness_is_stated_with_how_stale(path):
    text = path.read_text()
    assert "warehouse freshness" in text
    assert re.search(r"not `?pass`?", text), "any outcome but pass, not just fail"
    assert "how stale" in text


@pytest.mark.parametrize("path", [QUERY, ANALYST], ids=lambda p: p.name)
def test_liveness_is_mentioned_as_the_reason_to_rerun(path):
    assert "loader liveness" in path.read_text()


def test_claude_md_has_the_one_line():
    text = CLAUDE_MD.read_text()
    assert re.search(r"[Bb]efore reporting (anything )?from the warehouse, run `stdtel-doctor", text)
