"""Regressions for defects found by auditing hooks against real transcripts and docs.

Each test names the failure it prevents; several of these bugs were silent by
construction (hooks exit 0 on purpose), so only a test makes them visible.
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel import exporter as exp_mod
from stdtel.enrich import detect_harness, resource_attributes
from stdtel.hooks import cli as hooks
from stdtel.hooks.cli import _resolve, _skill_from_payload
from stdtel.transcript import read_slice
from tests.test_manifest import GOOD


# --- 1. empty transcript_path used to raise IsADirectoryError and lose everything ---

def test_read_slice_tolerates_empty_path():
    assert read_slice(Path("")).skill_loads == []          # Path("") == "." == a directory
    assert read_slice(Path("/nope/missing.jsonl")).requests == []


def test_stop_without_transcript_still_emits(tmp_path):
    sid = "no-transcript"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    e = InMemorySpanExporter()
    # invocation + session cost (the Skill call itself counts as a tool call)
    assert hooks.stop({"session_id": sid}, exporter=e) == 2      # was 0, silently
    span = next(s for s in e.get_finished_spans()
                if s.attributes.get("std.artefact.kind") == "skill")
    assert span.attributes["std.skill.load_tokens"] == 0


# --- 2/3. the endpoint never reached the hook, and the flush never timed out ---

def test_endpoint_prefers_stdtel_var_because_otel_vars_are_scrubbed(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-only:4318")
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://collector:4318")
    assert exp_mod._endpoint() == "http://collector:4318/v1/traces"


@pytest.mark.parametrize("env,expected", [
    ({}, "http://localhost:4318/v1/traces"),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://a:4318/"}, "http://a:4318/v1/traces"),
    ({"STDTEL_OTLP_ENDPOINT": "http://b:4318/v1/traces"}, "http://b:4318/v1/traces"),
])
def test_endpoint_forms(monkeypatch, env, expected):
    for k in ("STDTEL_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT",
              "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert exp_mod._endpoint() == expected


def test_timeout_is_bounded(monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_TIMEOUT", raising=False)
    assert exp_mod._timeout() == exp_mod.DEFAULT_TIMEOUT_S     # never unbounded
    monkeypatch.setenv("STDTEL_OTLP_TIMEOUT", "5")
    assert exp_mod._timeout() == 5
    monkeypatch.setenv("STDTEL_OTLP_TIMEOUT", "nonsense")
    assert exp_mod._timeout() == exp_mod.DEFAULT_TIMEOUT_S


def test_exporter_carries_the_timeout(monkeypatch):
    """force_flush(timeout_millis=) is ignored upstream (#4043); only this works."""
    monkeypatch.setenv("STDTEL_OTLP_TIMEOUT", "1")
    assert exp_mod._otlp_exporter()._timeout == 1


# --- 4. Copilot reads .claude/settings.json, so its events arrived as claude-code ---

def test_camelcase_payload_is_detected_as_copilot():
    assert detect_harness({"sessionId": "s", "toolName": "Skill"}) == "copilot"


def test_snake_case_is_not_claimed_for_copilot():
    assert detect_harness({"session_id": "s", "tool_name": "Skill"}) is None
    assert detect_harness({}) is None and detect_harness(None) is None


def test_detected_harness_beats_the_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_HARNESS", "claude-code")
    assert resource_attributes(tmp_path, {"sessionId": "s"})["std.harness"] == "copilot"
    assert resource_attributes(tmp_path, {"session_id": "s"})["std.harness"] == "claude-code"


# --- 5. plugin skills arrive namespaced; 71% of real invocations look like this ---

def test_namespaced_skill_resolves_to_the_catalogue(tmp_path, monkeypatch):
    root = tmp_path / "skills" / "structured-logging"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(GOOD.replace("name: x", "name: structured-logging"))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path / "skills"))
    cat = hooks._catalogue()
    for invoked in ("structured-logging", "my-plugin:structured-logging"):
        manifest, resolved = _resolve(invoked, cat)
        assert manifest is not None, invoked
        assert resolved == "structured-logging"
    assert _resolve("other-plugin:unknown", cat)[0] is None


def test_namespaced_invocation_keeps_plugin_and_version(tmp_path):
    sid = "ns"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "my-plugin:structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid}, exporter=e)
    span = next(s for s in e.get_finished_spans()
                if s.attributes.get("std.artefact.kind") == "skill")
    assert span.attributes["std.skill.name"] == "structured-logging"
    assert span.attributes["std.skill.invoked_as"] == "my-plugin:structured-logging"
    assert span.attributes["std.skill.plugin"] == "my-plugin"
    assert span.attributes["std.skill.version"] == "2.3.0"      # was "unversioned"


# --- 6. trigger was a constant: no real payload has a slash or an "explicit" key ---

def test_trigger_is_not_invented():
    assert _skill_from_payload({"tool_input": {"skill": "a"}}) == ("a", "unknown")
    assert _skill_from_payload({"tool_input": {"skill": "/a"}}) == ("a", "explicit")
    assert _skill_from_payload({"tool_input": {"skill": "a"},
                                "caller": {"type": "direct"}}) == ("a", "direct")


def test_trigger_comes_from_the_transcript(tmp_path):
    """The tool_use caller block is ground truth; the hook payload may lack it."""
    t = tmp_path / "t.jsonl"
    t.write_text(
        '{"type":"assistant","timestamp":"2026-09-10T00:00:00Z","message":{"model":"m",'
        '"content":[{"type":"tool_use","id":"t1","name":"Skill",'
        '"input":{"skill":"structured-logging"},"caller":{"type":"direct"}}]}}\n')
    sid = "trig"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=e)
    assert e.get_finished_spans()[0].attributes["std.skill.trigger"] == "direct"


# --- 7. the hot path must never import OpenTelemetry or PyYAML ---

def test_hook_module_import_stays_stdlib_only():
    """PreToolUse/PostToolUse fire on every Skill call; a module-level import
    of the exporter costs ~31ms per invocation. Guard it."""
    code = ("import sys, stdtel.hooks.cli; "
            "print([m for m in sys.modules if m.split('.')[0] in ('opentelemetry', 'yaml')])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", f"heavy imports leaked: {out.stdout}"


# --- issue #1: a session that loads no skill must still report its cost ---

def test_session_with_no_skill_still_emits_cost(tmp_path):
    """Without this there is no denominator for cost-per-PR: most sessions load
    no skill, and they used to emit nothing at all."""
    from tests.test_transcript import make_transcript
    t = tmp_path / "t.jsonl"
    make_transcript(t)
    sid = "cost-only"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    e = InMemorySpanExporter()
    assert hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=e) == 1
    (span,) = e.get_finished_spans()
    assert span.name == "std.session.cost"
    assert span.attributes["std.session.llm_requests"] > 0
    assert span.attributes["gen_ai.usage.input_tokens"] > 0


def test_session_cost_is_the_total_not_the_tail(tmp_path):
    """Session cost and skill tail deliberately overlap; the total must be >= the
    tail, and the two must never be summed."""
    from tests.test_transcript import make_transcript
    t = tmp_path / "t.jsonl"
    make_transcript(t)
    sid = "overlap"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=e)
    by_name = {s.name: s for s in e.get_finished_spans()}
    by_kind = {s.attributes.get("std.artefact.kind"): s for s in e.get_finished_spans()}
    total = by_name["std.session.cost"].attributes["gen_ai.usage.input_tokens"]
    tail = by_kind["skill"].attributes["gen_ai.usage.input_tokens"]
    assert total >= tail > 0


def test_empty_slice_emits_nothing(tmp_path):
    """A second Stop with no new transcript content must stay silent."""
    sid = "quiet"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    assert hooks.stop({"session_id": sid, "transcript_path": ""},
                      exporter=InMemorySpanExporter()) == 0


def test_session_duration_is_a_duration_not_an_epoch(tmp_path):
    """A transcript timestamp that parses to ~0 turned active_seconds into
    'seconds since 1970'. Session start must come from state."""
    from tests.test_transcript import make_transcript
    t = tmp_path / "t.jsonl"
    make_transcript(t)
    sid = "dur"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=e)
    span = e.get_finished_spans()[0]
    seconds = (span.end_time - span.start_time) / 1e9
    assert 0 <= seconds < 60, f"implausible session duration: {seconds}s"


# --- issue #2: tool-call failure rate, without leaking tool content ---

def test_non_skill_tool_calls_are_counted(tmp_path):
    sid = "tools"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    for tool, err in [("Bash", False), ("Bash", True), ("Read", False), ("Edit", False)]:
        hooks.post_tool_use({"session_id": sid, "tool_name": tool,
                             "tool_use_id": "x", "tool_response": "SECRET FILE CONTENT"},
                            error=err)
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": ""}, exporter=e)
    a = e.get_finished_spans()[0].attributes
    assert a["std.session.tool_calls"] == 4
    assert a["std.session.tool_failures"] == 1
    assert a["std.session.tool.Bash.calls"] == 2
    assert a["std.session.tool.Bash.failures"] == 1
    assert "std.session.tool.Read.failures" not in a       # zero failures stay off the wire


def test_tool_content_never_reaches_a_span(tmp_path):
    """The hook now sees every tool, so tool_response carries file contents,
    commands and diffs. Counts only."""
    sid = "noleak"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Bash", "tool_use_id": "x",
                         "tool_input": {"command": "cat /etc/passwd"},
                         "tool_response": "root:x:0:0:SECRET"})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": ""}, exporter=e)
    blob = repr(e.get_finished_spans()[0].attributes)
    for banned in ("passwd", "SECRET", "root:x", "cat /etc"):
        assert banned not in blob, f"leaked {banned!r}"


def test_scrub_refuses_tool_content_attributes():
    from stdtel.exporter import scrub
    out = scrub({"tool_input": "x", "tool_response": "y", "tool.result": "z",
                 "std.tool.output": "w", "std.session.tool_calls": 3})
    assert out == {"std.session.tool_calls": 3}


def test_tool_counts_drain_so_a_second_stop_does_not_double_count(tmp_path):
    sid = "drain"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Bash", "tool_use_id": "x"})
    e1 = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": ""}, exporter=e1)
    assert e1.get_finished_spans()[0].attributes["std.session.tool_calls"] == 1
    e2 = InMemorySpanExporter()
    assert hooks.stop({"session_id": sid, "transcript_path": ""}, exporter=e2) == 0


def test_every_state_field_survives_a_save_load_cycle():
    """Each hook is a separate process. A field that save() forgets reads as its
    default at the next event — silently, as a plausible zero."""
    import dataclasses
    from stdtel.state import SessionState
    st = SessionState.load("roundtrip")
    st.transcript_offset, st.started_at = 4242, 1700000000.5
    st.resource = {"std.team": "payments"}
    st.record_tool("Bash", failed=True)
    st.open_window("s", "1.0.0", "direct", "t1", prompt_id="p1", permission_mode="default")
    st.save()
    back = SessionState.load("roundtrip")
    for f in dataclasses.fields(SessionState):
        if f.name == "session_id":
            continue
        assert getattr(back, f.name) == getattr(st, f.name), f"{f.name} lost across save/load"


# --- opt-out controls: advertised since the first commit, wired up now ---

def test_global_kill_switch_stops_everything(tmp_path, monkeypatch, capsys):
    """STDTEL_DISABLED must short-circuit before the payload is even parsed."""
    from stdtel.hooks.cli import disabled, main
    monkeypatch.setenv("STDTEL_DISABLED", "1")
    assert disabled() is True
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"x","tool_name":"Skill"}'))
    assert main(["pre-tool-use"]) == 0
    assert not list(Path(os.environ["STDTEL_STATE_DIR"]).glob("*.json")) \
        if Path(os.environ["STDTEL_STATE_DIR"]).exists() else True


@pytest.mark.parametrize("value,off", [("1", True), ("true", True), ("YES", True), ("on", True),
                                       ("0", False), ("false", False), ("", False)])
def test_kill_switch_accepts_the_obvious_spellings(monkeypatch, value, off):
    from stdtel.hooks.cli import disabled
    monkeypatch.setenv("STDTEL_DISABLED", value)
    assert disabled() is off


def test_skill_with_telemetry_emit_false_is_not_attributed(tmp_path, monkeypatch):
    """The SKILL.md opt-out was parsed and validated but read by nothing."""
    root = tmp_path / "skills" / "quiet-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        GOOD.replace("name: x", "name: quiet-skill").replace(
            "---\nbody", "telemetry:\n  emit: false\n---\nbody"))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path / "skills"))
    sid = "quiet"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "quiet-skill"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    e = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": ""}, exporter=e)
    names = [s.name for s in e.get_finished_spans()]
    kinds = {s.attributes.get("std.artefact.kind") for s in e.get_finished_spans()}
    assert "skill" not in kinds, "opted-out skill was still attributed"
    assert "std.session.cost" in names, "session cost is an aggregate and still reported"
