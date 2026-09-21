"""Containment must be recorded, not merely claimed (ADR-010, #75).

Every span used to be started with no parent context, which makes it the root of
its own trace. ADR-009 stated the opposite — "every activation is a child of the
session" — and that sentence survived drafting, council review, acceptance and a
live end-to-end verification pass, because a flat trace and a parented one are
indistinguishable in every other check here: the `session.id` attribute is
present either way, the loader reads attributes rather than structure, and the
dashboards group by label.

So these tests exist to make structure fail loudly. The parent is the *turn*,
not the session: a session is resumed and persists, and a trace spanning weeks
outgrows what Tempo will hold.
"""
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel import artefact
from stdtel.exporter import emit_activations, build_provider


def activation(kind, *, prompt_id=None, parent_prompt_id=None, start=1.0, end=2.0):
    attrs = {"std.artefact.kind": kind, "std.artefact.source": "hook"}
    if prompt_id:
        attrs["std.prompt.id"] = prompt_id
    if parent_prompt_id:
        attrs["std.artefact.parent_prompt_id"] = parent_prompt_id
    if kind != artefact.KIND_TURN:
        attrs["std.artefact.name"] = f"a-{kind}"
    return {"kind": kind, "started_at": start, "ended_at": end, "attributes": attrs}


def emit(acts):
    exp = InMemorySpanExporter()
    emit_activations(build_provider({}, exporter=exp), acts, "sess-1")
    return {s.attributes.get("std.artefact.kind"): s for s in exp.get_finished_spans()}, exp


def test_the_fixture_actually_produces_the_spans_these_tests_assert_on():
    """Non-vacuity. This suite has shipped assertions that matched nothing before."""
    spans, exp = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                       activation(artefact.KIND_SKILL, prompt_id="p1")])
    assert len(exp.get_finished_spans()) == 2
    assert artefact.KIND_TURN in spans and artefact.KIND_SKILL in spans


def test_a_skill_is_a_child_of_the_turn_it_ran_in():
    """The defect this is all about: without a parent, "what did this turn cost
    including its skills" is not a question the data can answer."""
    spans, _ = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                     activation(artefact.KIND_SKILL, prompt_id="p1")])
    turn, skill = spans[artefact.KIND_TURN], spans[artefact.KIND_SKILL]
    assert skill.parent is not None, "a skill emitted as a root records no containment at all"
    assert skill.parent.span_id == turn.get_span_context().span_id
    assert skill.context.trace_id == turn.get_span_context().trace_id, \
        "parent and child in different traces is a flat emit wearing a parent id"


def test_a_subagent_is_a_child_of_the_turn_that_spawned_it():
    """A sub-agent names its parent turn in `parent_prompt_id` rather than
    `prompt.id`, so the lookup has to consider both or sub-agents stay flat."""
    spans, _ = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                     activation(artefact.KIND_SUBAGENT, parent_prompt_id="p1")])
    assert spans[artefact.KIND_SUBAGENT].parent.span_id == \
        spans[artefact.KIND_TURN].get_span_context().span_id


def test_a_turn_is_a_root_because_the_session_is_not_a_bounded_unit():
    """ADR-010's first rejected option. A session is resumed and persists — one
    real session spans seven weeks — so a trace rooted at the session outgrows
    Tempo's per-trace limits and late spans land in fragments."""
    spans, _ = emit([activation(artefact.KIND_TURN, prompt_id="p1")])
    assert spans[artefact.KIND_TURN].parent is None


def test_a_compaction_is_not_given_an_invented_parent():
    """Compaction happens between turns. Attaching it to whichever turn was
    nearby would be recording containment that did not occur (ADR-005)."""
    spans, _ = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                     activation(artefact.KIND_COMPACTION)])
    assert spans[artefact.KIND_COMPACTION].parent is None


def test_an_activation_whose_turn_is_absent_stays_a_root():
    """A skill can begin before the last Stop, so its turn is not in this batch.
    Guessing a parent would attribute its cost to an unrelated turn."""
    spans, _ = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                     activation(artefact.KIND_SKILL, prompt_id="p-missing")])
    assert spans[artefact.KIND_SKILL].parent is None


def test_children_of_one_turn_share_that_turn_s_trace():
    """The point of parenting: one bounded trace per turn, queryable as a unit."""
    spans, exp = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                       activation(artefact.KIND_SKILL, prompt_id="p1"),
                       activation(artefact.KIND_SUBAGENT, parent_prompt_id="p1")])
    trace_ids = {s.context.trace_id for s in exp.get_finished_spans()}
    assert len(trace_ids) == 1, f"expected one trace for the turn, got {len(trace_ids)}"


def test_two_turns_are_two_traces():
    """Bounded means bounded: turns do not share a trace, or the trace grows for
    the life of the session and we are back at the rejected option."""
    spans, exp = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                       activation(artefact.KIND_TURN, prompt_id="p2")])
    assert len({s.context.trace_id for s in exp.get_finished_spans()}) == 2


def test_every_span_still_carries_the_session_join():
    """Parentage replaces nothing. The session join stays an attribute, which is
    how ticket, repo and team continue to join as they do today."""
    _spans, exp = emit([activation(artefact.KIND_TURN, prompt_id="p1"),
                        activation(artefact.KIND_SKILL, prompt_id="p1")])
    for s in exp.get_finished_spans():
        assert s.attributes["session.id"] == "sess-1"


# --- the real hook path, not just the emit helper -----------------------------

def test_parentage_holds_through_the_actual_stop_hook(tmp_path):
    """The unit tests above drive `emit_activations` directly, so they would pass
    even if the hook built its activations with attribute names that never match.
    This drives the path a session actually takes."""
    from stdtel.hooks import cli as hooks

    sid = "parent-e2e"
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": "2026-09-15T10:00:00.000Z",
                    "promptId": "p1", "message": {"content": []}}),
        json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:01.000Z",
                    "message": {"model": "claude-opus-5", "content": [],
                                "usage": {"input_tokens": 1, "output_tokens": 9,
                                          "cache_read_input_tokens": 0,
                                          "cache_creation_input_tokens": 0}}}),
    ]) + "\n")

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    # `prompt_id` is present on real PreToolUse payloads — see
    # tests/fixtures/hook_payloads.json, captured from a live session. It is what
    # ties a skill to its turn, and omitting it here hid the link entirely.
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "prompt_id": "p1", "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                         "prompt_id": "p1"})

    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)

    by_kind = {}
    for s in exp.get_finished_spans():
        by_kind.setdefault(s.attributes.get("std.artefact.kind"), []).append(s)
    turns = by_kind.get(artefact.KIND_TURN, [])
    skills = by_kind.get(artefact.KIND_SKILL, [])
    assert turns and skills, f"fixture produced no turn/skill to check: {sorted(by_kind)}"

    turn_ids = {t.get_span_context().span_id for t in turns}
    for skill in skills:
        assert skill.parent is not None, \
            "the real path still emits skills as roots; the unit tests passed anyway"
        assert skill.parent.span_id in turn_ids


def test_a_skill_with_no_prompt_id_degrades_to_a_root_rather_than_guessing(tmp_path):
    """`prompt_id` is on every current payload, but a harness that omits it must
    cost us containment, not correctness. Attaching the skill to whichever turn
    happened to be open would attribute its tokens to an unrelated prompt."""
    from stdtel.hooks import cli as hooks

    sid = "parent-noprompt"
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "timestamp": "2026-09-15T10:00:00.000Z",
         "promptId": "p1", "message": {"content": []}}) + "\n")

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})

    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    skills = [s for s in exp.get_finished_spans()
              if s.attributes.get("std.artefact.kind") == artefact.KIND_SKILL]
    assert skills and all(s.parent is None for s in skills)
