"""ADR-009 capture, end to end through the hooks.

Sub-agents were the largest unmeasured cost in a real session ($366, 42
sub-agent runs, one reading 3.2M cached tokens, none of it recorded), so these
tests exist to keep that from being true again — and to keep the content that
sits next to those numbers off the wire.
"""
import json
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks
from stdtel.state import SessionState

DOCUMENTED = json.loads(
    (Path(__file__).parent / "fixtures" / "hook_payloads_documented.json").read_text())

SECRET = "SHOULD-NEVER-BE-EXPORTED"


def _entry(**kw):
    kw.setdefault("timestamp", "2026-09-15T10:00:00.000Z")
    return json.dumps(kw)


def write_transcript(path: Path, lines) -> None:
    path.write_text("\n".join(lines) + "\n")


def assistant(ts, output=10, model="claude-opus-5", tools=()):
    content = [{"type": "tool_use", "name": t, "id": f"t{i}", "input": {}}
               for i, t in enumerate(tools)]
    return _entry(type="assistant", timestamp=ts,
                  message={"model": model, "usage": {"input_tokens": 1, "output_tokens": output,
                                                     "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0},
                           "content": content})


def user_turn(ts, prompt_id):
    return _entry(type="user", timestamp=ts, promptId=prompt_id, message={"content": []})


def kinds(exporter):
    out = {}
    for s in exporter.get_finished_spans():
        out.setdefault(s.attributes.get("std.artefact.kind"), []).append(s)
    return out


def all_attribute_values(exporter):
    values = []
    for s in exporter.get_finished_spans():
        values.append(s.name)
        for k, v in s.attributes.items():
            values.extend([str(k), str(v)])
        for k, v in (s.resource.attributes or {}).items():
            values.extend([str(k), str(v)])
    return values


# --------------------------------------------------------------- sub-agents

def _subagent_transcript(tmp_path: Path, sid: str, agent_id: str, *, with_content=False) -> Path:
    d = tmp_path / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"agent-{agent_id}.jsonl"
    body = [{"type": "text", "text": SECRET}] if with_content else []
    write_transcript(path, [
        _entry(type="assistant", timestamp="2026-09-15T10:00:01.000Z",
               message={"model": "claude-haiku-4-5-20251001", "content": body,
                        "usage": {"input_tokens": 3, "output_tokens": 5,
                                  "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 0}}),
        _entry(type="assistant", timestamp="2026-09-15T10:00:02.000Z",
               message={"model": "claude-haiku-4-5-20251001",
                        "content": [{"type": "tool_use", "name": "Bash", "id": "x", "input": {"command": SECRET}}],
                        "usage": {"input_tokens": 2, "output_tokens": 4,
                                  "cache_read_input_tokens": 2000,
                                  "cache_creation_input_tokens": 0}}),
    ])
    (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": SECRET, "spawnDepth": 1}))
    return path


def test_a_subagent_run_is_captured_with_its_cost(tmp_path, monkeypatch):
    sid = "sa-1"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-09-15T10:00:00.000Z")])
    sub = _subagent_transcript(tmp_path, sid, "ag-1")

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.subagent_start({"session_id": sid, "agent_id": "ag-1", "agent_type": "Explore",
                          "prompt_id": "p1", "agent_transcript_path": str(sub)})
    hooks.subagent_stop({"session_id": sid, "agent_id": "ag-1", "agent_type": "Explore",
                         "prompt_id": "p1", "agent_transcript_path": str(sub),
                         "last_assistant_message": SECRET})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)

    spans = kinds(exp)["subagent"]
    assert len(spans) == 1
    a = spans[0].attributes
    assert a["std.subagent.type"] == "Explore" and a["std.artefact.name"] == "Explore"
    assert a["std.artefact.source"] == "hook"
    assert a["std.artefact.parent_prompt_id"] == "p1"
    # summed per request, not cumulative: 3+2 in, 5+4 out, 1000+2000 cache read
    assert a["gen_ai.usage.input_tokens"] == 5
    assert a["gen_ai.usage.output_tokens"] == 9
    assert a["gen_ai.usage.cache_read_input_tokens"] == 3000
    assert a["std.subagent.llm_requests"] == 2
    assert a["std.subagent.tool_calls"] == 1


def test_no_part_of_a_subagent_payload_or_transcript_reaches_a_span(tmp_path):
    """The leak test ADR-009 asks for.

    `last_assistant_message` is the sub-agent's reply, `agent_transcript_path`
    is this machine's filesystem layout, the meta file's `description` is the
    task it was given, and the transcript holds message bodies and a shell
    command. Content is planted in every one of them, including a field no
    version of the harness documents.
    """
    sid = "sa-leak"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-09-15T10:00:00.000Z")])
    sub = _subagent_transcript(tmp_path, sid, "ag-2", with_content=True)

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.subagent_stop({
        "session_id": sid, "agent_id": "ag-2", "agent_type": "Explore",
        "agent_transcript_path": str(sub),
        "last_assistant_message": SECRET,
        "undocumented_future_field": SECRET,
        "nested": {"deeper": SECRET},
    })
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)

    assert kinds(exp).get("subagent"), "the run must still be measured"
    for value in all_attribute_values(exp):
        assert SECRET not in value, f"content leaked into telemetry: {value!r}"
    assert str(sub) not in all_attribute_values(exp), "a transcript path is filesystem layout"


def test_the_directory_is_scanned_only_while_the_hook_has_never_fired(tmp_path):
    """A harness without SubagentStop still gets measured, and says so.

    The fallback is an inference, not an observation, so it is flagged
    `source=transcript` — and it must not double-count once the real hook is
    firing on this machine.
    """
    sid = "sa-3"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-09-15T10:00:00.000Z")])
    _subagent_transcript(tmp_path, sid, "ag-3")

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    found = kinds(exp)["subagent"]
    assert len(found) == 1 and found[0].attributes["std.artefact.source"] == "transcript"
    assert found[0].attributes["std.subagent.type"] == "Explore", "read from the meta file"

    # a second Stop must not report the same run again
    write_transcript(transcript, [assistant("2026-09-15T10:00:00.000Z"),
                                  assistant("2026-09-15T10:00:05.000Z")])
    exp2 = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp2)
    assert not kinds(exp2).get("subagent")


# -------------------------------------------------------------------- turns

def test_a_turn_is_captured_per_prompt_with_its_hook_latency(tmp_path):
    sid = "turn-1"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [
        user_turn("2026-09-15T10:00:00.000Z", "p-one"),
        assistant("2026-09-15T10:00:01.000Z", output=10, tools=("Bash", "Read")),
        _entry(type="system", subtype="turn_duration", timestamp="2026-09-15T10:00:02.000Z",
               durationMs=1500),
        _entry(type="system", subtype="stop_hook_summary", timestamp="2026-09-15T10:00:02.100Z",
               hookInfos=[{"command": "/opt/hooks/cc-status", "durationMs": 40},
                          {"command": "node /opt/hooks/graft.cjs stop", "durationMs": 60}]),
        user_turn("2026-09-15T10:00:10.000Z", "p-two"),
        assistant("2026-09-15T10:00:11.000Z", output=20),
    ])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript),
                "permission_mode": "default"}, exporter=exp)

    turns = {s.attributes["std.prompt.id"]: s for s in kinds(exp)["turn"]}
    assert set(turns) == {"p-one", "p-two"}
    one = turns["p-one"].attributes
    assert one["gen_ai.usage.output_tokens"] == 10
    assert one["std.turn.tool_calls"] == 2
    assert one["std.turn.duration_ms"] == 1500
    assert one["std.turn.hook_ms"] == 100
    assert one["std.turn.hook.cc-status.ms"] == 40
    assert one["std.turn.hook.graft-cjs.ms"] == 60
    assert turns["p-two"].attributes["gen_ai.usage.output_tokens"] == 20


def test_tokens_before_the_first_boundary_go_to_the_turn_that_was_open(tmp_path):
    """Stop fires inside a turn, so the next slice opens mid-turn. Without the
    carry-over those tokens are either dropped or folded into the next turn."""
    sid = "turn-2"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [user_turn("2026-09-15T10:00:00.000Z", "p-one"),
                                  assistant("2026-09-15T10:00:01.000Z", output=10)])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)},
               exporter=InMemorySpanExporter())
    assert SessionState.load(sid).open_prompt_id == "p-one"

    with transcript.open("a") as f:      # more of the same turn, no new boundary
        f.write(assistant("2026-09-15T10:00:03.000Z", output=7) + "\n")
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    later = kinds(exp)["turn"]
    assert len(later) == 1
    assert later[0].attributes["std.prompt.id"] == "p-one"
    # the delta, not a restated total: rows for one prompt_id are summed
    assert later[0].attributes["gen_ai.usage.output_tokens"] == 7


def test_a_slice_with_no_observed_prompt_id_emits_no_turn(tmp_path):
    sid = "turn-3"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-09-15T10:00:01.000Z")])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    assert not kinds(exp).get("turn")
    assert kinds(exp).get(None), "the session total is still reported"


# --------------------------------------------------------------- compaction

def test_a_compaction_is_captured_from_the_hook(tmp_path):
    sid = "compact-1"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [user_turn("2026-09-15T10:00:00.000Z", "p1"),
                                  assistant("2026-09-15T10:00:01.000Z")])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)},
               exporter=InMemorySpanExporter())
    hooks.post_compact({"session_id": sid, "compaction_reason": "auto",
                        "token_count_estimate_before": 967334,
                        "token_count_estimate_after": 13177})
    with transcript.open("a") as f:
        f.write(assistant("2026-09-15T10:00:05.000Z") + "\n")
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)

    c = kinds(exp)["compaction"][0].attributes
    assert c["std.compaction.reason"] == "auto" and c["std.artefact.name"] == "auto"
    assert c["std.compaction.tokens_before"] == 967334
    assert c["std.compaction.tokens_after"] == 13177
    assert c["std.compaction.turns_since_previous"] == 1
    assert c["std.artefact.source"] == "hook"


def test_a_compaction_without_estimates_reports_none_rather_than_zero(tmp_path):
    sid = "compact-2"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-09-15T10:00:01.000Z")])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.post_compact({"session_id": sid, "compaction_reason": "manual"})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    c = kinds(exp)["compaction"][0].attributes
    assert "std.compaction.tokens_before" not in c
    assert "std.compaction.tokens_after" not in c


def test_the_transcript_supplies_compactions_until_the_hook_does(tmp_path):
    sid = "compact-3"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [
        assistant("2026-09-15T10:00:01.000Z"),
        _entry(type="system", subtype="compact_boundary", timestamp="2026-09-15T10:00:02.000Z",
               compactMetadata={"trigger": "auto", "preTokens": 900, "postTokens": 100}),
    ])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    c = kinds(exp)["compaction"][0].attributes
    assert c["std.artefact.source"] == "transcript"
    assert c["std.compaction.tokens_before"] == 900


def test_the_transcript_fallback_stops_once_the_hook_has_fired(tmp_path):
    """Both sources describing one compaction would double-count it."""
    sid = "compact-4"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [
        assistant("2026-09-15T10:00:01.000Z"),
        _entry(type="system", subtype="compact_boundary", timestamp="2026-09-15T10:00:02.000Z",
               compactMetadata={"trigger": "auto", "preTokens": 900, "postTokens": 100}),
    ])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.post_compact({"session_id": sid, "compaction_reason": "auto",
                        "token_count_estimate_before": 900,
                        "token_count_estimate_after": 100})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    assert len(kinds(exp)["compaction"]) == 1


# ------------------------------------------------------------- session cost

def test_the_sessions_real_cost_reaches_the_session_span(tmp_path):
    """`session_cost.cost_usd` was NULL for every row in the warehouse because
    nothing read the harness's own `cost-state` entry."""
    sid = "cost-1"
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [
        assistant("2026-09-15T10:00:01.000Z"),
        _entry(type="cost-state", totalCostUSD=366.2183, totalDuration=305499513,
               totalAPIDuration=23863724, totalToolDuration=7939522),
    ])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    cost = next(s for s in exp.get_finished_spans() if s.name == "std.session.cost").attributes
    assert cost["std.session.cost_usd"] == 366.2183
    assert cost["std.session.api_ms"] == 23863724
    assert cost["std.session.tool_ms"] == 7939522


# ------------------------------------------------------- honesty about what
# ------------------------------------------------------- has been observed

def test_the_documented_payloads_are_marked_as_not_captured():
    """These three payloads were transcribed from documentation, not observed.

    `tests/fixtures/hook_payloads.json` is the file that holds real captures and
    it does not contain them. Conflating the two would let a documented field
    name pass for evidence that capture works.
    """
    assert "NOT captured" in DOCUMENTED["_provenance"]
    real = json.loads((Path(__file__).parent / "fixtures" / "hook_payloads.json").read_text())
    assert not ({"SubagentStart", "SubagentStop", "PostCompact"} & set(real))


def test_the_documented_payloads_drive_the_hooks_they_describe(tmp_path):
    """Shape-compatible with what the docs say the harness sends."""
    sid = DOCUMENTED["SubagentStop"]["session_id"]
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.subagent_start(DOCUMENTED["SubagentStart"])
    hooks.subagent_stop(DOCUMENTED["SubagentStop"])
    hooks.post_compact(DOCUMENTED["PostCompact"])
    st = SessionState.load(sid)
    assert st.observed_events == ["subagent-start", "subagent-stop", "post-compact"]
    assert st.compactions[0].tokens_before == 125000
    assert [s.agent_type for s in st.subagents] == ["Explore"]
