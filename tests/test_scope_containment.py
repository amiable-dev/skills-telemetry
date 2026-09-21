"""The container a loop skill's work belongs to (ADR-010, phase 3 of #78).

A loop skill's own activation is seconds of tool call. Everything expensive
happens underneath it, across hundreds of turns. Without a container its
recorded cost is near zero while the work it drives is the largest line item.

Containment across turns is carried by attributes rather than a span tree: a
session is resumed and persists, so a tree rooted there outgrows what Tempo will
hold. `std.scope.name` is the scoping artefact and is stable for the whole run;
`std.scope.key` is the unit instance and rolls every iteration. So comparing
iterations and totalling a run are the same rows grouped differently.
"""
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.artefact import SCOPE_ID, SCOPE_KEY, SCOPE_NAME, SCOPE_SOURCE
from stdtel.hooks import cli as hooks
from stdtel.state import SessionState

SKILL = """---
name: {name}
description: d
metadata:
  version: "1.0.0"
  standard_id: STD-TEL-001
  policy_ids: "a.b"
  owner: platform
  harness_support: "claude-code"
{scope}---

body
"""


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    root = tmp_path / "cat"

    def add(name, scope=None):
        d = root / name
        d.mkdir(parents=True)
        line = f"  telemetry.scope: {scope}\n" if scope else ""
        (d / "SKILL.md").write_text(SKILL.format(name=name, scope=line))

    add("epic-loop", scope="ticket")
    add("formatter")                       # declares nothing: turn-scoped
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(root))
    return root


def use_skill(sid, name, tool_use_id, prompt_id="p1"):
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tool_use_id,
                        "prompt_id": prompt_id, "tool_input": {"skill": name}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tool_use_id,
                         "prompt_id": prompt_id})


def run(sid, cwd) -> list:
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(cwd)}, exporter=exp)
    return list(exp.get_finished_spans())


def activations(spans):
    return [s for s in spans if s.attributes.get("std.artefact.kind")]


def scopes(spans, attr=SCOPE_KEY):
    return {s.attributes.get(attr) for s in activations(spans)}


def test_a_skill_that_declares_nothing_opens_no_container(catalogue, tmp_path, monkeypatch):
    """Nothing is required of any author (ADR-011). Undeclared is turn-scoped,
    and a turn already has real span parentage — it needs no attribute."""
    monkeypatch.setenv("STDTEL_BRANCH", "STDTEL-1-x")
    sid = "scope-none"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "formatter", "t1")
    assert scopes(run(sid, tmp_path)) == {None}
    assert SessionState.load(sid).scope_name == ""


def test_a_ticket_scoped_skill_opens_a_container_on_the_current_ticket(catalogue, tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    sid = "scope-open"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    spans = run(sid, tmp_path)
    assert scopes(spans, SCOPE_NAME) == {"epic-loop"}
    assert scopes(spans, SCOPE_KEY) == {"STDTEL-11"}
    assert all(s.attributes.get(SCOPE_ID) for s in activations(spans))


def test_the_key_rolls_when_the_ticket_moves_without_a_new_activation(catalogue, tmp_path, monkeypatch):
    """The case the whole design is for. A loop skill activates once and runs for
    hundreds of turns; the ticket moves every iteration with nothing to announce
    it. Without this the entire run stays on the key it opened with and every
    iteration looks like one."""
    sid = "scope-roll"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    first = run(sid, tmp_path)
    assert scopes(first) == {"STDTEL-11"}

    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-12-two")
    # the loop does ordinary work in the next iteration; note it is a *different*,
    # unscoped skill, so the container comes from state rather than this activation
    use_skill(sid, "formatter", "t2", prompt_id="p2")
    second = run(sid, tmp_path)
    assert scopes(second) == {"STDTEL-12"}, "the second iteration still reads as the first"


def test_the_scope_name_survives_the_roll_so_a_run_can_be_totalled(catalogue, tmp_path, monkeypatch):
    """Iterations and the run are the same rows grouped differently: by key for
    one iteration, by name for the whole thing."""
    sid = "scope-total"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    run(sid, tmp_path)
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-12-two")
    use_skill(sid, "formatter", "t2", prompt_id="p2")
    assert scopes(run(sid, tmp_path), SCOPE_NAME) == {"epic-loop"}


def test_the_id_changes_on_a_roll_so_two_visits_to_a_ticket_stay_separable(catalogue, tmp_path, monkeypatch):
    sid = "scope-id"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    run(sid, tmp_path)
    first_id = SessionState.load(sid).scope_id
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-12-two")
    use_skill(sid, "formatter", "t2", prompt_id="p2")
    run(sid, tmp_path)
    assert SessionState.load(sid).scope_id != first_id


def test_a_spans_scope_key_never_contradicts_its_own_ticket(catalogue, tmp_path, monkeypatch):
    """The boundary Stop, where a loop skill's per-iteration cost actually lands.

    The ticket refreshes per Stop, so the spans in the Stop that follows a branch
    change already carry the new `std.ticket.id`. Stamping them with the key the
    scope opened on would make a span disagree with itself, which is worse than a
    boundary that is one turn out.
    """
    sid = "scope-boundary"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    run(sid, tmp_path)

    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-12-two")
    use_skill(sid, "formatter", "t2", prompt_id="p2")
    for s in activations(run(sid, tmp_path)):
        assert s.attributes[SCOPE_KEY] == s.resource.attributes["std.ticket.id"]


def test_a_second_scoping_skill_supersedes_the_first(catalogue, tmp_path, monkeypatch):
    """ADR-010's second termination condition. One scope level only: nesting is a
    known limitation, not a half-built feature."""
    (catalogue / "other-loop").mkdir()
    (catalogue / "other-loop" / "SKILL.md").write_text(
        SKILL.format(name="other-loop", scope="  telemetry.scope: ticket\n"))
    sid = "scope-supersede"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    run(sid, tmp_path)
    use_skill(sid, "other-loop", "t2")
    assert scopes(run(sid, tmp_path), SCOPE_NAME) == {"other-loop"}


def test_everything_in_the_window_inherits_the_scope_not_just_the_skill(catalogue, tmp_path, monkeypatch):
    """A sub-agent inside a loop iteration is as much that iteration's cost as
    the skill that opened it. There is no observable causal link from a skill to
    a later tool call, so containment is the honest unit (ADR-010 decision 9)."""
    sid = "scope-window"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    sub = tmp_path / sid / "subagents"
    sub.mkdir(parents=True)
    agent = sub / "agent-a1.jsonl"
    agent.write_text(json.dumps(
        {"type": "assistant", "timestamp": "2026-09-15T10:00:01.000Z",
         "message": {"model": "claude-haiku-4-5-20251001", "content": [],
                     "usage": {"input_tokens": 3, "output_tokens": 5,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}}) + "\n")
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore", "spawnDepth": 1}))

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    hooks.subagent_start({"session_id": sid, "agent_id": "a1", "agent_type": "Explore",
                          "prompt_id": "p1", "agent_transcript_path": str(agent)})
    hooks.subagent_stop({"session_id": sid, "agent_id": "a1", "agent_type": "Explore",
                         "prompt_id": "p1", "agent_transcript_path": str(agent)})
    spans = run(sid, tmp_path)
    kinds = {s.attributes["std.artefact.kind"] for s in activations(spans)}
    assert "subagent" in kinds, f"fixture produced no sub-agent: {kinds}"
    assert scopes(spans) == {"STDTEL-11"}, "a sub-agent inside the iteration was left unscoped"


def test_the_session_cost_span_is_never_scoped(catalogue, tmp_path, monkeypatch):
    """It covers the whole session, which spans many containers. Scoping it would
    claim a total that belongs to no one of them — and it is already the span
    nothing may be summed with."""
    sid = "scope-session"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "epic-loop", "t1")
    session = [s for s in run(sid, tmp_path) if not s.attributes.get("std.artefact.kind")]
    assert session, "fixture emitted no session-cost span"
    assert all(SCOPE_KEY not in s.attributes for s in session)


def test_an_overlay_supplied_scope_is_marked_as_assumed(catalogue, tmp_path, monkeypatch):
    """An overlay is a local claim about someone else's artefact. It can be wrong
    or go stale, so an analyst must be able to separate what an artefact said
    about itself from what we assumed for it (ADR-011)."""
    overlay = tmp_path / "overlay" / "third-party"
    overlay.mkdir(parents=True)
    (overlay / "SKILL.md").write_text(SKILL.format(
        name="third-party", scope="  telemetry.scope: ticket\n"))
    plain = catalogue / "third-party"
    plain.mkdir()
    (plain / "SKILL.md").write_text(SKILL.format(name="third-party", scope=""))
    monkeypatch.setenv("STDTEL_SKILLS_OVERLAY", str(tmp_path / "overlay"))

    sid = "scope-overlay"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-11-one")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    use_skill(sid, "third-party", "t1")
    assert scopes(run(sid, tmp_path), SCOPE_SOURCE) == {"overlay"}


def test_the_scope_survives_a_save_load_cycle(catalogue, tmp_path):
    """Each hook is a separate process, so an unpersisted scope reads as "no
    container" at the next event and the run silently stops being measured."""
    st = SessionState.load("scope-persist")
    st.open_scope("epic-loop", "ticket", "STDTEL-11", "artefact")
    st.save()
    back = SessionState.load("scope-persist")
    assert (back.scope_name, back.scope_unit, back.scope_key, back.scope_source) == \
        ("epic-loop", "ticket", "STDTEL-11", "artefact")
    assert back.scope_id == st.scope_id
