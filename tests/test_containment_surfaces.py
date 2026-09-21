"""The surfaces that carry containment to a reader (phases 5-7 of #78).

A number nobody can find is not surfaced, and a number surfaced without its
caveat is worse than absent. ADR-010's sharpest risk is that a containment
rollup gets read as an effect — it is large, it has a skill's name on it, and
this project's whole primary metric is causal. So the warning has to travel with
the number, on every surface that shows it.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ONBOARD = (ROOT / "skills" / "stdtel-onboard" / "SKILL.md").read_text()
ANALYST = (ROOT / "agents" / "skill-scorecard-analyst.md").read_text()
WALKTHROUGH = (ROOT / "docs" / "insight-walkthroughs.md").read_text()
OUTCOMES = json.loads(
    (ROOT / "deploy" / "grafana" / "provisioning" / "dashboards"
     / "skill-scorecard-outcomes.json").read_text())


def containment_panel():
    for p in OUTCOMES["panels"]:
        if "inclusive" in json.dumps(p).lower():
            return p
    return None


# --- phase 5: one skill decorates all three artefact kinds --------------------

@pytest.mark.parametrize("artefact", ["skill", "sub-agent", "MCP server"])
def test_the_onboarding_skill_covers_every_artefact_type(artefact):
    """Extended rather than duplicated (ADR-011): a sibling skill would split one
    workflow across two things a user has to know to look for."""
    assert artefact in ONBOARD


def test_the_onboarding_skill_says_a_sub_agent_block_is_only_tolerated():
    """`metadata` is not a documented agent key. It works because unknown keys are
    ignored, and that could end in any release — silently, with agents simply
    going back to undecorated. Someone deciding to rely on it deserves to know."""
    assert "tolerance" in ONBOARD or "tolerated" in ONBOARD
    assert "overlay" in ONBOARD, "the fallback has to be named next to the risk"


def test_the_onboarding_skill_refuses_session_scope_with_a_reason():
    """It is the value the author of a long-running skill reaches for first, and
    the one ADR-010 rejects outright."""
    assert "`session` is not a value" in ONBOARD
    assert "resumed and persists" in ONBOARD


def test_the_onboarding_skill_says_scope_is_for_the_few_not_the_many():
    """A gate is what ADR-004's `policy_ids` scar is about. If this reads as
    something every artefact ought to set, authors will claim a wider unit than
    they run in and every rollup inflates."""
    assert "Most skills need nothing here" in ONBOARD


# --- phase 7: the caveat travels with the number ------------------------------

def test_the_analyst_is_told_containment_is_not_causation():
    """The failure mode to guard hardest: the number is large, it has a name
    attached, and reading it as an effect reproduces this dataset's cardinal
    error one level up."""
    assert "Containment is not causation" in ANALYST
    assert "incurred under" in ANALYST


def test_the_analyst_keeps_the_verdict_on_the_causal_metric():
    """A containment total must never stand in for first-time pass rate, at any
    volume — that is the one substitution this whole brief exists to prevent."""
    section = ANALYST.split("The third question")[1]
    assert "with and\nwithout" in section or "with and without" in section
    assert "sample-size floor" in section


def test_the_containment_panel_exists_and_warns_against_summing():
    """self is inside inclusive. Adding them double-counts, exactly like the
    session-cost and skill-tail pair this repo already documents."""
    panel = containment_panel()
    assert panel is not None, "nothing on the outcomes board shows containment"
    blob = json.dumps(panel)
    assert "never add the columns" in blob.lower()
    assert "caused" in blob, "the panel must say what it does not answer"


def test_the_containment_panel_says_empty_is_normal():
    """No artefact declaring a scope is the expected state, not a fault. An empty
    panel that looks broken sends someone hunting a problem that is not there."""
    assert "normal state and not a fault" in json.dumps(containment_panel())


def test_the_walkthrough_shows_the_misreadings_not_just_the_query():
    """`docs/insight-walkthroughs.md` exists because this dataset invites specific
    wrong conclusions; a new question without its traps is half-documented."""
    section = WALKTHROUGH.split("What did a loop skill really cost?")[1]
    for trap in ("As causation", "By summing the two columns",
                 "as though they were equal work", "overlay row"):
        assert trap in section, f"the walkthrough does not warn about: {trap}"


def test_the_walkthrough_explains_a_zero_self_cost():
    """A loop activates once and then runs, so zero is the common case. Left
    unexplained it reads as missing data and undermines the whole row."""
    assert "not a bug" in WALKTHROUGH.split("What did a loop skill really cost?")[1]


# --- the boundary the doctor must not overstate -------------------------------

def test_the_onboarding_skill_says_scope_is_a_skill_field_only():
    """An agent may carry the key and nothing reads it. Implying otherwise sends
    someone to decorate an agent, watch the doctor report adoption, and get no
    span carrying it — an advertised control that does nothing."""
    assert "This field applies to skills" in ONBOARD
    assert "no container is opened for an agent" in ONBOARD.lower()


def test_a_scope_declared_on_an_agent_is_dropped_and_not_counted(tmp_path, monkeypatch):
    """Found by review, not by a test: `partial_manifest` learned the field for
    overlays, agents load through it, and the doctor counted them — so a
    decorated agent reported as scoped while no span ever carried it."""
    from stdtel.doctor import scope_adoption
    from stdtel.manifest import load_agent_catalogue

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "looper.md").write_text(
        '---\nname: looper\ndescription: d\ntools: Bash\n'
        'metadata:\n  version: "1.0.0"\n  telemetry.scope: ticket\n---\n\nbody\n')
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(agents))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path / "no-skills"))

    assert load_agent_catalogue(agents)["looper"].scope == "", \
        "an agent's scope must be dropped where it is loaded, not carried inertly"
    assert "0 of 0 skill(s) declare a scope" in scope_adoption().detail, \
        "the doctor must not report adoption that changes no span"
