"""Sample-size arithmetic and the doc it generates.

The published figures must be reproducible, so these tests pin the formulas
against hand-computed values and assert the committed doc is not stale.
"""
import math
import subprocess
import sys
from pathlib import Path

import pytest

from eval.power import (BLOCK, DOC, TABLES, design_effect, n_paired_by_developer,
                        n_token_ratio, n_two_proportion, render, z)

ROOT = Path(__file__).resolve().parent.parent


def test_z_values_match_the_published_constants():
    assert round(z(0.975), 3) == 1.960
    assert round(z(0.80), 3) == 0.842


def test_two_proportion_matches_the_worked_example():
    """The substitution shown in the doc: 60% -> 75% is ~149.1, so 150."""
    assert n_two_proportion(0.60, 0.75) == 150


def test_smaller_effects_cost_more():
    assert n_two_proportion(0.60, 0.70) > n_two_proportion(0.60, 0.75) > n_two_proportion(0.60, 0.80)


def test_variance_shrinks_near_the_extremes():
    """Same 15-point lift is cheaper from a higher baseline."""
    assert n_two_proportion(0.70, 0.85) < n_two_proportion(0.60, 0.75)


def test_no_effect_is_an_error():
    with pytest.raises(ValueError):
        n_two_proportion(0.6, 0.6)


def test_design_effect_formula():
    assert design_effect(10, 0.2) == pytest.approx(2.8)
    assert design_effect(1, 0.9) == 1.0, "a single PR per developer means no clustering"


def test_clustering_dominates_the_token_answer():
    """The headline: ICC moves the requirement by more than 2x."""
    low, high = n_token_ratio(0.9, 0.30, 0.1, 10), n_token_ratio(0.9, 0.30, 0.4, 10)
    assert high / low > 2


def test_token_ratio_matches_the_published_figure():
    assert n_token_ratio(0.9, 0.30, 0.2, 10) == 396


def test_effect_enters_quadratically():
    """Halving the effect quadruples the requirement."""
    assert n_token_ratio(0.9, 0.20, 0.2, 10) / n_token_ratio(0.9, 0.40, 0.2, 10) == pytest.approx(4, abs=0.05)


def test_developer_pairing_is_the_wrong_framing():
    """71 pairs looks cheaper than 396 PRs but is far more work: each pair is a
    developer-fortnight, so it is 710 PRs at 10 PRs per developer."""
    pairs = n_paired_by_developer(0.9, 0.30)
    assert pairs == 71
    assert pairs * 10 > n_token_ratio(0.9, 0.30, 0.2, 10)


# --- the doc must stay in sync ---

def test_every_table_has_a_placeholder():
    names = {m.group("name") for m in BLOCK.finditer(DOC.read_text())}
    assert names == set(TABLES), f"placeholder/table mismatch: {names ^ set(TABLES)}"


def test_no_generated_block_is_empty():
    """A regex bug once left every block empty while reporting success."""
    text, filled = render(DOC.read_text())
    assert sorted(filled) == sorted(TABLES)
    for name in TABLES:
        body = text.split(f"<!-- generated:{name} -->")[1].split("<!-- /generated -->")[0]
        assert "|" in body, f"{name} rendered no table"


def test_committed_doc_is_not_stale():
    r = subprocess.run([sys.executable, "-m", "eval.power", "--check"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_hard_floor_is_stated():
    """The floor is quoted verbatim in the agent and stdtel-query; it must exist here."""
    text = " ".join(DOC.read_text().split())      # collapse wrapping
    assert "below 30 merged PRs per arm, or fewer than 5 developers" in text


# --- issue #4: the floor must read identically everywhere it appears ---

def test_hard_floor_is_stated_verbatim_on_every_surface():
    """A floor worded three ways is three floors, and the loosest one wins.

    Compared with whitespace collapsed, because markdown wraps.
    """
    from eval.power import FLOOR_SURFACES, HARD_FLOOR
    for rel in FLOOR_SURFACES:
        text = " ".join((ROOT / rel).read_text().split())
        assert HARD_FLOOR in text, f"{rel} does not state the floor verbatim"


def test_readme_states_the_three_phases_before_the_layout():
    """Someone who never opens the power doc must still meet this."""
    text = (ROOT / "README.md").read_text()
    section = text.split("## Layout")[0]
    assert "When can I trust these numbers?" in section
    for phase in ("Descriptive", "Directional", "Inferential"):
        assert phase in section, f"missing phase: {phase}"


def test_phases_are_framed_by_volume_not_calendar_time():
    """Calendar time depends on team size; volume does not."""
    section = (ROOT / "README.md").read_text().split("## Layout")[0]
    assert "data volume, not elapsed time" in section
    for banned in ("Days 1-30", "first month", "after two weeks of use"):
        assert banned not in section, f"phase framed by calendar time: {banned}"


def test_agent_definition_documents_refusal_as_correct():
    """Checks the agent's *instructions*, not its behaviour.

    Named for what it does. The previous name — "the agent treats refusal as a
    correct answer" — claimed to verify conduct while reading a markdown file for
    a substring, which is the same overclaiming this project fixes elsewhere.
    Nothing here runs the agent or observes what it does with thin data.

    Verifying the behaviour needs a model in the loop: see ADR-007 and issue #10
    for `claude plugin eval`, whose `llm` graders can put the question to the
    agent and judge the reply.
    """
    text = (ROOT / "agents" / "skill-scorecard-analyst.md").read_text()
    assert "## When to refuse" in text
    assert "correct answer, not a failure" in text
