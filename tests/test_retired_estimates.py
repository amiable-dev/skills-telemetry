"""#117, ADR-014 decision 10: stdtel's hooks stop attributing tokens to skills.

The tail rule (every request after a skill loaded, until the next), its
first-only sensitivity check and chars/4 `load_tokens` were estimates of what
Claude Code now records per request, with the skill named on it. They were due
for removal one release after native records were verified live (0.8.0). A
skill activation still records that the skill ran, its identity and its scope;
what it cost is `skill_request_cost`.

Removing chars/4 also removes the one place stdtel read the length of a Skill
tool result's content.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel import artefact, transcript
from stdtel.hooks import cli as hooks
from tests.test_transcript import make_transcript

ROOT = Path(__file__).resolve().parent.parent
RETIRED_ATTRS = ("std.skill.load_tokens", "std.skill.tail_tokens", "std.skill.tail_tokens_first_only",
                 "std.skill.llm_requests")
RETIRED_COLS = ("load_tokens", "tail_tokens", "tail_tokens_first_only", "llm_requests", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_creation_tokens", "model")


@pytest.fixture
def skill_span(tmp_path):
    t = tmp_path / "t.jsonl"
    make_transcript(t)
    sid = "retired"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(t)}, exporter=exp)
    spans = exp.get_finished_spans()
    return ([s for s in spans if s.attributes.get("std.artefact.kind") == "skill"][0],
            {s.attributes.get("std.artefact.kind") or s.name: s for s in spans})


def test_a_skill_span_carries_no_token_estimate(skill_span):
    span, _ = skill_span
    for key in RETIRED_ATTRS:
        assert key not in span.attributes, key
    assert not [k for k in span.attributes if k.startswith("gen_ai.usage.")]
    assert "gen_ai.request.model" not in span.attributes, "the tail's model was tail-derived too"


def test_a_skill_span_still_says_what_ran(skill_span):
    span, _ = skill_span
    assert span.attributes["std.skill.name"] == "structured-logging"
    assert span.attributes["std.skill.version"] == "2.3.0"
    assert span.attributes["gen_ai.tool.name"] == "Skill"


def test_the_session_keeps_its_observed_counts(skill_span):
    """The transcript's own usage totals, not an attribution. (Turns keep theirs
    too; this fixture has no prompt ids, so it makes no turn span.)"""
    _, by = skill_span
    assert by["std.session.cost"].attributes["gen_ai.usage.input_tokens"] > 0


def test_the_allowlist_refuses_the_retired_keys_on_a_skill():
    for key in RETIRED_ATTRS + ("gen_ai.usage.input_tokens", "gen_ai.request.model"):
        assert key not in artefact.ALLOWED[artefact.KIND_SKILL], key


def test_the_transcript_no_longer_estimates_or_attributes():
    assert not hasattr(transcript, "attribute") and not hasattr(transcript, "Attribution")
    assert "load_tokens" not in transcript.SkillLoad.__dataclass_fields__


def test_the_loader_and_schema_drop_the_columns():
    from tests.test_warehouse import schema_columns
    spec = importlib.util.spec_from_file_location("lt_ret", ROOT / "warehouse" / "load_traces.py")
    lt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lt)
    for col in RETIRED_COLS:
        assert col not in lt.COLS, col
        assert col not in schema_columns("skill_invocation"), col


def test_an_existing_warehouse_loses_the_columns():
    try:
        import psycopg
        conn = psycopg.connect("postgresql://postgres:stdtel@localhost:5432/stdtel", connect_timeout=3)
    except Exception as e:                           # noqa: BLE001
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")
    with conn, conn.cursor() as cur:
        cur.execute((ROOT / "warehouse" / "schema.sql").read_text())
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'skill_invocation'")
        have = {r[0] for r in cur.fetchall()}
    for col in RETIRED_COLS:
        assert col not in have, col


def test_a_state_file_written_before_the_upgrade_still_loads(tmp_path, monkeypatch):
    """A session open across the upgrade has `load_tokens` in its saved windows.
    `SkillWindow(**w)` raised on the unknown key, and the hook — which exits 0 —
    would have dropped that session's skills in silence."""
    import json
    from stdtel.state import SessionState, state_dir
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (state_dir() / "old.json").write_text(json.dumps({"windows": [
        {"skill": "structured-logging", "version": "2.3.0", "trigger": "direct", "started_at": 1.0,
         "tool_use_id": "t1", "load_tokens": 812, "error": False, "a_field_from_the_future": 1}]}))
    st = SessionState.load("old")
    assert [w.skill for w in st.windows] == ["structured-logging"]
