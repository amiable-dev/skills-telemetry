"""Spooling spans to disk so capture does not depend on the collector (ADR-008).

Today `stop` opens a socket. A dead collector costs the developer the timeout and
loses the data anyway — measured at 7.34s of retry backoff before it was bounded,
0.91s and gone after. A hook that never talks to the network cannot stall on it,
which is what "never block the developer" was always reaching for.

The spool must not reintroduce silent loss: records survive a failed drain, are
removed only after success, and a bounded spool counts what it drops.
"""
import json
import os
from pathlib import Path

import pytest

from stdtel.spool import (SpoolFull, append, drain, read_all, spool_path, trim)


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_SPOOL_DIR", str(tmp_path / "spool"))
    return tmp_path / "spool"


def record(n=1):
    return {"name": "std.artefact.activation", "kind": "skill", "session_id": f"s{n}",
            "started_at": 1.0, "ended_at": 2.0,
            "attributes": {"std.skill.name": "x"}, "resource": {"std.team": "t"}}


# --- writing never touches the network ---

def test_append_writes_one_json_object_per_line(spool):
    append([record(1), record(2)])
    lines = [l for l in spool_path().read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_append_makes_no_network_call(spool, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: pytest.fail("spooling opened a socket"))
    append([record()])


def test_append_scrubs_content_before_it_reaches_disk(spool):
    """The content rules apply to disk, not only to the wire."""
    leaky = record()
    leaky["attributes"]["gen_ai.prompt"] = "SECRET PROMPT"
    leaky["attributes"]["tool_response"] = "SECRET FILE"
    append([leaky])
    text = spool_path().read_text()
    assert "SECRET" not in text
    assert "std.skill.name" in text


# --- draining is the only thing that may delete ---

def test_drain_removes_records_only_after_a_successful_export(spool):
    append([record(1), record(2)])
    sent = []
    assert drain(lambda batch: sent.extend(batch)) == 2
    assert not read_all(), "spool should be empty after a successful drain"
    assert len(sent) == 2


def test_a_failed_export_leaves_every_record_in_place(spool):
    append([record(1), record(2)])
    def boom(batch):
        raise ConnectionError("collector down")
    with pytest.raises(ConnectionError):
        drain(boom)
    assert len(read_all()) == 2, "a failed drain must not lose data"


def test_draining_twice_does_not_duplicate(spool):
    append([record(1)])
    seen = []
    drain(lambda b: seen.extend(b))
    drain(lambda b: seen.extend(b))
    assert len(seen) == 1


def test_records_appended_during_a_drain_are_not_lost(spool):
    """A hook can fire while the exporter is running."""
    append([record(1)])
    def export(batch):
        append([record(99)])          # a hook fires mid-drain
    drain(export)
    remaining = read_all()
    assert len(remaining) == 1 and remaining[0]["session_id"] == "s99"


# --- bounded, and loud about it ---

def test_trim_drops_oldest_first_and_reports_the_count(spool):
    append([record(i) for i in range(10)])
    dropped = trim(max_records=4)
    assert dropped == 6
    kept = [r["session_id"] for r in read_all()]
    assert kept == ["s6", "s7", "s8", "s9"], kept


def test_trim_is_a_no_op_below_the_bound(spool):
    append([record(1)])
    assert trim(max_records=10) == 0


def test_a_corrupt_line_does_not_take_the_spool_down(spool):
    append([record(1)])
    with spool_path().open("a") as fh:
        fh.write("{not json\n")
    append([record(2)])
    assert len(read_all()) == 2, "readable records must survive one bad line"


# --- the hook path: spooling instead of exporting ---

def test_stop_spools_instead_of_exporting_when_asked(spool, tmp_path, monkeypatch):
    """STDTEL_SPOOL=1 makes the hook write to disk and open no socket."""
    import socket
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from stdtel.hooks import cli as hooks
    monkeypatch.setenv("STDTEL_SPOOL", "1")
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: pytest.fail("stop opened a socket while spooling"))
    sid = "spooled"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    assert hooks.stop({"session_id": sid, "transcript_path": ""}) > 0
    rows = read_all()
    assert rows, "nothing was spooled"
    assert any(r["name"] == "std.artefact.activation" for r in rows)


def test_spooled_records_carry_what_the_exporter_needs(spool, tmp_path, monkeypatch):
    from stdtel.hooks import cli as hooks
    monkeypatch.setenv("STDTEL_SPOOL", "1")
    sid = "shape"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    hooks.stop({"session_id": sid, "transcript_path": ""})
    row = next(r for r in read_all() if r["name"] == "std.artefact.activation")
    for field in ("session_id", "started_at", "ended_at", "attributes", "resource"):
        assert field in row, f"spooled record lacks {field}"


def test_export_from_spool_round_trips_to_real_spans(spool, tmp_path, monkeypatch):
    """What is drained must become the same spans a direct export would produce."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from stdtel.hooks import cli as hooks
    from stdtel.spool_export import export_batch
    monkeypatch.setenv("STDTEL_SPOOL", "1")
    sid = "rt"
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1",
                        "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": "t1"})
    hooks.stop({"session_id": sid, "transcript_path": ""})
    exp = InMemorySpanExporter()
    n = drain(lambda batch: export_batch(batch, exporter=exp))
    assert n > 0
    names = {s.name for s in exp.get_finished_spans()}
    assert "std.artefact.activation" in names
    span = next(s for s in exp.get_finished_spans()
                if s.attributes.get("std.artefact.kind") == "skill")
    assert span.attributes["std.skill.name"] == "structured-logging"
    assert not read_all(), "a successful export must clear the spool"


def test_the_bound_is_configurable_and_survives_a_bad_value(spool, monkeypatch):
    from stdtel.spool import max_records_default
    monkeypatch.setenv("STDTEL_SPOOL_MAX", "5")
    assert max_records_default() == 5
    monkeypatch.setenv("STDTEL_SPOOL_MAX", "nonsense")
    assert max_records_default() > 0
    append([record(i) for i in range(8)])
    monkeypatch.setenv("STDTEL_SPOOL_MAX", "5")
    assert trim() == 3
