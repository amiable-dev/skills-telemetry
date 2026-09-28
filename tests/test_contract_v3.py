"""ADR-012: external contract v3 (issue #94).

Two halves of one join. The emitter sends the `toolUseId` Claude Code gave its
MCP server; stdtel records the same id from the transcript of the session that
made the call. Joined at read time, a council run lands on the exact turn that
caused it, whatever `/clear` did to the session id the server read at launch.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel import artefact, conform
from stdtel.hooks import cli as hooks
from stdtel.transcript import attribute_turns, read_slice, summarise_subagent

ROOT = Path(__file__).resolve().parent.parent
IDS = "std.artefact.mcp_tool_use_ids"


def _entry(**kw):
    kw.setdefault("timestamp", "2026-09-28T10:00:00.000Z")
    return json.dumps(kw)


def assistant(ts, tools=(), model="claude-opus-5"):
    content = [{"type": "tool_use", "name": name, "id": tid, "input": {}} for name, tid in tools]
    return _entry(type="assistant", timestamp=ts,
                  message={"model": model, "content": content,
                           "usage": {"input_tokens": 1, "output_tokens": 2,
                                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})


def user_turn(ts, prompt_id):
    return _entry(type="user", timestamp=ts, promptId=prompt_id, message={"content": []})


def write(path: Path, lines) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def loader():
    spec = importlib.util.spec_from_file_location("load_traces_v3", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- capture: the transcript half ---------------------------------------------------

def test_the_transcript_keeps_mcp_tool_use_ids_and_nothing_else(tmp_path):
    """Only `mcp__*` calls: they are the only ones an external emitter can
    receive a toolUseId for. Keeping every tool's id would put an unbounded list
    on every turn to serve no join."""
    t = write(tmp_path / "s.jsonl", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", tools=[("mcp__llm-council__consult_council", "toolu_A"),
                                                      ("Bash", "toolu_B"), ("Skill", "toolu_C")]),
    ])
    sl = read_slice(t, 0)
    assert [tid for _ts, tid in sl.mcp_calls] == ["toolu_A"]
    assert len(sl.tool_uses) == 3, "the tool count is unchanged"


def test_each_turn_claims_the_mcp_calls_made_inside_it(tmp_path):
    t = write(tmp_path / "s.jsonl", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", tools=[("mcp__c__consult", "toolu_1")]),
        user_turn("2026-09-28T10:00:10.000Z", "p2"),
        assistant("2026-09-28T10:00:11.000Z", tools=[("mcp__c__verify", "toolu_2"), ("mcp__c__consult", "toolu_3")]),
    ])
    # `now` as the Stop hook passes it; the last window otherwise ends on its own final entry
    turns = {x.prompt_id: x for x in attribute_turns(read_slice(t, 0), now=2_000_000_000.0)}
    assert turns["p1"].mcp_tool_use_ids == ["toolu_1"]
    assert turns["p2"].mcp_tool_use_ids == ["toolu_2", "toolu_3"]


def test_the_turn_span_carries_the_ids_as_a_list(tmp_path):
    sid = "v3-turn"
    t = write(tmp_path / f"{sid}.jsonl", [
        user_turn("2026-09-28T10:00:00.000Z", "p1"),
        assistant("2026-09-28T10:00:01.000Z", tools=[("mcp__c__consult", "toolu_1"), ("Bash", "toolu_x")]),
        user_turn("2026-09-28T10:00:10.000Z", "p2"),
        assistant("2026-09-28T10:00:11.000Z", tools=[("Read", "toolu_y")]),
    ])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=exp)
    turns = {s.attributes["std.prompt.id"]: s.attributes for s in exp.get_finished_spans()
             if s.attributes.get("std.artefact.kind") == "turn"}
    assert tuple(turns["p1"][IDS]) == ("toolu_1",), "scrub() must not drop a list"
    assert IDS not in turns["p2"], "no MCP call means no attribute, never an empty list"


def test_a_subagents_mcp_calls_are_read_from_its_own_transcript(tmp_path):
    """Checked on real data: a sub-agent's MCP call is recorded only in the
    sub-agent's transcript, never the parent's."""
    sub = write(tmp_path / "agent-a1.jsonl", [
        assistant("2026-09-28T10:00:01.000Z", tools=[("mcp__c__consult", "toolu_S")]),
    ])
    assert summarise_subagent(sub).mcp_tool_use_ids == ["toolu_S"]
    act = artefact.subagent("a1", "Explore", 1.0, 2.0, {}, 1, 1, mcp_tool_use_ids=["toolu_S"])
    assert act["attributes"][IDS] == ["toolu_S"]


def test_scrub_keeps_a_list_of_strings_but_nothing_richer():
    from stdtel.exporter import scrub
    out = scrub({"a": ["toolu_1", "toolu_2"], "b": [{"x": 1}], "c": ["ok", 3], "d": "s"})
    assert out == {"a": ["toolu_1", "toolu_2"], "d": "s"}


def test_an_empty_list_is_an_unobserved_value():
    act = artefact.turn("p1", 1.0, 2.0, {}, llm_requests=1, mcp_tool_use_ids=[])
    assert IDS not in act["attributes"]


# --- the contract ---------------------------------------------------------------------

def ext(**kw):
    base = dict(system="llm-council", operation="consult", started_at=1.0, ended_at=2.0)
    return artefact.external(**{**base, **kw})["attributes"]


def test_the_emitter_sends_the_tool_use_id_it_was_given():
    assert ext(tool_use_id="toolu_A")["std.external.tool_use_id"] == "toolu_A"
    assert "std.external.tool_use_id" not in ext()


def test_unpriced_requests_are_a_count_and_zero_is_observed():
    """Zero means "every request was priced" — a measurement, not an absence."""
    assert ext(requests_unpriced=2)["std.external.requests_unpriced"] == 2
    assert ext(requests_unpriced=0)["std.external.requests_unpriced"] == 0
    assert "std.external.requests_unpriced" not in ext()
    with pytest.raises(ValueError):
        ext(requests_unpriced=-1)


def test_the_contract_is_version_three():
    assert conform.CONTRACT_VERSION == 3
    c = conform.contract()
    assert {"std.external.tool_use_id", "std.external.requests_unpriced"} <= set(c["attributes"])


# --- the checker ------------------------------------------------------------------------

def span(**attrs):
    base = {"std.artefact.kind": "external", "std.external.system": "llm-council",
            "std.external.operation": "consult", "std.artefact.source": "emitter"}
    kv = []
    for k, v in {**base, **attrs}.items():
        if isinstance(v, bool):
            kv.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            kv.append({"key": k, "value": {"intValue": str(v)}})
        elif isinstance(v, float):
            kv.append({"key": k, "value": {"doubleValue": v}})
        elif v is None:
            kv.append({"key": k, "value": {}})
        else:
            kv.append({"key": k, "value": {"stringValue": v}})
    return {"name": "std.artefact.activation", "attributes": kv}


def check(*spans):
    return conform.check({"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]})


def test_a_partial_run_is_not_counted_as_reported():
    """A lower bound does not reconcile to an invoice. It is kept — that is the
    point of v3 — and counted on its own."""
    r = check(span(**{"std.external.cost_usd": 1.0, "std.external.requests_unpriced": 1}),
              span(**{"std.external.cost_usd": 1.0, "std.external.requests_unpriced": 0}))
    assert r.ok, [p.message for p in r.problems]
    assert r.cost_reported == 1
    assert r.cost_partial == 1
    assert "lower bound" in r.summary()


def test_the_checker_refuses_a_malformed_count_or_id():
    bad = [m.message for m in check(span(**{"std.external.requests_unpriced": -2})).problems]
    assert any("requests_unpriced" in m for m in bad)
    bad = [m.message for m in check(span(**{"std.external.requests_unpriced": "2"})).problems]
    assert any("requests_unpriced" in m for m in bad)
    bad = [m.message for m in check(span(**{"std.external.tool_use_id": None})).problems]
    assert any("tool_use_id" in m for m in bad)
    every = [m.message for s in ({"std.external.requests_unpriced": -2}, {"std.external.requests_unpriced": "2"},
                                  {"std.external.tool_use_id": None})
             for m in check(span(**s)).problems]
    assert not any("allowlist" in m for m in every), "must fail for its reason, not as a stray key"


def test_an_attribute_value_that_is_an_otlp_integer_string_is_read_as_an_integer():
    """OTLP JSON carries intValue as a string. Reading it as text would refuse
    every correct emitter."""
    r = check(span(**{"std.external.requests_unpriced": 3}))
    assert r.ok, [p.message for p in r.problems]


# --- the loader --------------------------------------------------------------------------

def _tempo(*spans):
    def val(v):
        if isinstance(v, list):
            return {"arrayValue": {"values": [{"stringValue": x} for x in v]}}
        if isinstance(v, int):
            return {"intValue": str(v)}
        return {"stringValue": v}
    out = []
    for i, (attrs, parent) in enumerate(spans):
        out.append({"name": "std.artefact.activation", "spanId": f"span{i}", "traceId": "t",
                    "parentSpanId": parent, "startTimeUnixNano": "1000000000",
                    "endTimeUnixNano": "2000000000",
                    "attributes": [{"key": k, "value": val(v)} for k, v in attrs.items()]})
    return {"batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": out}]}]}


def test_the_loader_reads_a_list_attribute():
    mod = loader()
    assert mod._attrs([{"key": "k", "value": {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}}}]) \
        == {"k": ["a", "b"]}


def test_the_loader_turns_carried_ids_into_one_row_per_call():
    mod = loader()
    calls: list = []
    acts, _, _ = mod.collect({"t": 1}, mcp_calls=calls, fetch=lambda _: _tempo(
        ({"std.artefact.kind": "turn", "std.prompt.id": "p1", "session.id": "S",
          IDS: ["toolu_1", "toolu_2"]}, ""),
        ({"std.artefact.kind": "subagent", "std.artefact.parent_prompt_id": "p1", "session.id": "S",
          IDS: ["toolu_3"]}, ""),
        ({"std.artefact.kind": "external", "std.external.system": "c", "session.id": "OLD",
          "std.external.tool_use_id": "toolu_1", "std.external.requests_unpriced": 1}, ""),
    ))
    rows = sorted(calls, key=lambda r: r["tool_use_id"])
    assert [(r["tool_use_id"], r["session_id"], r["prompt_id"]) for r in rows] == [
        ("toolu_1", "S", "p1"), ("toolu_2", "S", "p1"), ("toolu_3", "S", "p1")]
    ext_row = next(a for a in acts if a["kind"] == "external")
    assert ext_row["external_tool_use_id"] == "toolu_1"
    assert ext_row["external_requests_unpriced"] == 1
    assert ext_row["session_id"] == "OLD", "what the emitter said is kept, never overwritten"


def test_the_loader_and_the_contract_agree_on_v3():
    assert loader().LOADED_EXTERNAL_ATTRIBUTES == frozenset(artefact.ALLOWED[artefact.KIND_EXTERNAL])


def test_the_schema_holds_the_mapping_and_the_new_columns():
    schema = (ROOT / "warehouse" / "schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS mcp_tool_call" in schema
    for col in ("external_tool_use_id", "external_requests_unpriced"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in schema, col


# --- query 7 --------------------------------------------------------------------------------

Q7 = (ROOT / "warehouse" / "efficiency" / "07_external_spend_and_coverage.sql").read_text()


def test_query_seven_counts_partial_runs_and_how_many_joined():
    for col in ("runs_partial", "runs_joined_to_a_call"):
        assert col in Q7, col
    assert "mcp_tool_call" in Q7
    assert "version: 3" in Q7
    assert "%" not in Q7
