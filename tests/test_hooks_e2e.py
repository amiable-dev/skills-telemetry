"""End-to-end: simulate the hook lifecycle and assert on emitted spans (in-memory exporter)."""
import json, io, sys
from pathlib import Path
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from stdtel.hooks import cli as hooks
from stdtel.state import SessionState
from tests.test_transcript import make_transcript

def test_lifecycle(tmp_path, monkeypatch):
    sid = "sess-1"; transcript = tmp_path / "t.jsonl"; make_transcript(transcript)
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    st = SessionState.load(sid)
    # ADR-013: a branch identity, never a ticket; the repo is owner/name
    from stdtel.enrich import branch_hash
    expected = branch_hash("github.com/org/payments-api", "feature/PLAT-123-structured-logging")
    assert st.resource["std.branch.hash"] == expected and st.resource["std.repo"] == "org/payments-api"
    assert "std.ticket.id" not in st.resource

    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1", "tool_input": {"skill": "structured-logging"}})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Read", "tool_use_id": "zz", "tool_input": {}})  # ignored
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t2", "tool_input": {"skill": "other"}})
    # 'other' left open — Stop must close it

    exp = InMemorySpanExporter()
    n = hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    assert n == 3          # 2 skill invocations + 1 std.session.cost
    spans = {s.attributes["std.skill.name"]: s for s in exp.get_finished_spans()
             if s.attributes.get("std.artefact.kind") == "skill"}
    sl = spans["structured-logging"]
    assert sl.name == "std.artefact.activation"
    assert sl.attributes["std.skill.version"] == "2.3.0"
    assert sl.attributes["std.standard_id"] == "STD-LOG-001"
    assert sl.attributes["std.policy.ids"] == "logging.required_fields,logging.no_pii"
    # #117: a skill span says what ran, never what it cost (that is the harness's record)
    assert not [k for k in sl.attributes if k.startswith("gen_ai.usage.") or "tail_tokens" in k]
    assert sl.resource.attributes["std.branch.hash"] == expected
    assert sl.resource.attributes["std.harness"] == "claude-code"
    assert spans["other"].attributes["std.skill.version"] == "unversioned"
    # state drained; second stop emits nothing
    assert hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=InMemorySpanExporter()) == 0

def test_cli_never_fails(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hooks.main(["stop"]) == 0

def test_scrub_refuses_content():
    from stdtel.exporter import scrub
    out = scrub({"gen_ai.input.messages": "secret", "gen_ai.prompt": "x", "std.skill.name": "ok", "nested": {"a": 1}})
    assert out == {"std.skill.name": "ok"}
