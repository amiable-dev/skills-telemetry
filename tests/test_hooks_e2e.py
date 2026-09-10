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
    assert st.resource["std.ticket.id"] == "PLAT-123" and st.resource["std.repo"] == "payments-api"

    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1", "tool_input": {"skill": "structured-logging"}})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Read", "tool_use_id": "zz", "tool_input": {}})  # ignored
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t2", "tool_input": {"skill": "other"}})
    # 'other' left open — Stop must close it

    exp = InMemorySpanExporter()
    n = hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    assert n == 3          # 2 skill invocations + 1 std.session.cost
    spans = {s.attributes["std.skill.name"]: s for s in exp.get_finished_spans()
             if s.name == "std.skill.invocation"}
    sl = spans["structured-logging"]
    assert sl.name == "std.skill.invocation"
    assert sl.attributes["std.skill.version"] == "2.3.0"
    assert sl.attributes["std.standard_id"] == "STD-LOG-001"
    assert sl.attributes["std.policy.ids"] == "logging.required_fields,logging.no_pii"
    assert sl.attributes["std.skill.load_tokens"] == 100
    assert sl.attributes["std.skill.tail_tokens"] == 170
    assert sl.attributes["gen_ai.usage.cache_read_input_tokens"] == 100
    assert sl.resource.attributes["std.ticket.id"] == "PLAT-123"
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
