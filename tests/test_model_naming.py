"""Which model a span names, and when it names none (ADR-012 decision 5, applied
to our own spans).

Measured before writing this, over 1,157 real turns and 200 sub-agent runs:
every "multi-model" turn paired a real model with `<synthetic>`, the name
Claude Code gives placeholder assistant messages (an interrupted or failed
response, zero usage). One Prometheus series was already labelled
`gen_ai_request_model="<synthetic>"`. Genuinely multi-model runs exist only in
sub-agents (5 of 200), and naming the first of two models there says the run
used one.
"""
from __future__ import annotations

import json
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks
from stdtel.transcript import read_slice, single_model

ZERO = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
SOME = {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def assistant(ts, model, usage=SOME):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"model": model, "usage": usage, "content": []}})


def user_turn(ts, pid):
    return json.dumps({"type": "user", "timestamp": ts, "promptId": pid, "message": {"content": []}})


def write(p: Path, lines) -> Path:
    p.write_text("\n".join(lines) + "\n")
    return p


def test_a_synthetic_message_is_not_a_model_request(tmp_path):
    t = write(tmp_path / "s.jsonl", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", "<synthetic>", ZERO),
        assistant("2026-09-28T10:00:02.000Z", "claude-opus-5"),
    ])
    sl = read_slice(t, 0)
    assert sl.models() == ["claude-opus-5"]
    assert len(sl.requests) == 1, "a placeholder with zero usage made no model call"


def test_single_model_names_one_model_or_none():
    assert single_model(["claude-opus-5"]) == "claude-opus-5"
    assert single_model(["claude-fable-5", "claude-sonnet-5"]) == ""
    assert single_model([]) == ""


def _stop(tmp_path, sid, lines, sub_lines=None):
    t = write(tmp_path / f"{sid}.jsonl", lines)
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    if sub_lines is not None:
        d = tmp_path / sid / "subagents"
        d.mkdir(parents=True)
        write(d / "agent-a1.jsonl", sub_lines)
        (d / "agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore"}))
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=exp)
    return {s.attributes.get("std.artefact.kind"): s.attributes for s in exp.get_finished_spans()
            if s.attributes.get("std.artefact.kind")}


def test_a_turn_with_a_synthetic_message_still_names_its_real_model(tmp_path):
    """The case all 19 measured multi-model turns were. Omitting the model here
    would discard a correct name to fix a phantom."""
    spans = _stop(tmp_path, "mn-1", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", "<synthetic>", ZERO),
        assistant("2026-09-28T10:00:02.000Z", "claude-opus-5"),
    ])
    assert spans["turn"]["gen_ai.request.model"] == "claude-opus-5"
    assert spans["turn"]["std.turn.llm_requests"] == 1


def test_a_subagent_that_used_two_models_names_neither(tmp_path):
    spans = _stop(tmp_path, "mn-2", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", "claude-opus-5"),
    ], sub_lines=[
        assistant("2026-09-28T10:00:01.500Z", "claude-fable-5"),
        assistant("2026-09-28T10:00:01.800Z", "claude-sonnet-5"),
    ])
    assert "gen_ai.request.model" not in spans["subagent"], "the first of two models is not the model"
    assert spans["subagent"]["std.subagent.llm_requests"] == 2


def _skill_run(tmp_path, sid, tail_models):
    loads = json.dumps({"type": "assistant", "timestamp": "2026-09-28T10:00:01.000Z",
                        "message": {"model": tail_models[0], "usage": SOME, "content": [
                            {"type": "tool_use", "name": "Skill", "id": "t1", "input": {"skill": "structured-logging"}}]}})
    tail = [assistant(f"2026-09-28T10:00:0{2 + i}.000Z", m) for i, m in enumerate(tail_models)]
    t = write(tmp_path / f"{sid}.jsonl", [user_turn("2026-09-28T10:00:00.000Z", "p1"), loads, *tail])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=exp)
    return next(s.attributes for s in exp.get_finished_spans()
                if s.attributes.get("std.artefact.kind") == "skill")


def test_a_skill_tail_on_one_model_names_it(tmp_path):
    assert _skill_run(tmp_path, "mn-3", ["claude-opus-5", "claude-opus-5"])["gen_ai.request.model"] == "claude-opus-5"


def test_a_skill_tail_across_two_models_names_neither(tmp_path):
    assert "gen_ai.request.model" not in _skill_run(tmp_path, "mn-4", ["claude-opus-5", "claude-sonnet-5"])


def test_a_hook_observed_subagent_on_two_models_names_neither(tmp_path):
    """The SubagentStop path, which is preferred over the directory scan the
    test above reaches. Both build the span; both must apply the rule."""
    sid = "mn-5"
    t = write(tmp_path / f"{sid}.jsonl", [user_turn("2026-09-28T10:00:00.000Z", "p1"),
                                          assistant("2026-09-28T10:00:01.000Z", "claude-opus-5")])
    sub = write(tmp_path / "agent-h1.jsonl", [assistant("2026-09-28T10:00:01.500Z", "claude-fable-5"),
                                              assistant("2026-09-28T10:00:01.800Z", "claude-sonnet-5")])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.subagent_start({"session_id": sid, "agent_id": "h1", "agent_type": "Explore",
                          "prompt_id": "p1", "agent_transcript_path": str(sub)})
    hooks.subagent_stop({"session_id": sid, "agent_id": "h1", "agent_type": "Explore",
                         "prompt_id": "p1", "agent_transcript_path": str(sub)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=exp)
    a = next(s.attributes for s in exp.get_finished_spans()
             if s.attributes.get("std.artefact.kind") == "subagent")
    assert a["std.artefact.source"] == "hook", "this test is about the hook path"
    assert "gen_ai.request.model" not in a


def test_a_turn_on_two_real_models_names_neither(tmp_path):
    """Not seen in the 1,157 turns measured — every apparent case was
    `<synthetic>` — but a mid-turn model fallback would produce one, and the
    rule has to hold before the data shows it."""
    spans = _stop(tmp_path, "mn-6", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", "claude-opus-5"),
        assistant("2026-09-28T10:00:02.000Z", "claude-sonnet-5"),
    ])
    assert "gen_ai.request.model" not in spans["turn"]
