"""Concurrent hooks must not be able to kill a session's telemetry.

Found on a real session: 42 spans, then silence for a day. Its state file held a
complete JSON document followed by 235 bytes of a longer one — two hook
processes had written at the same time and their writes interleaved. `load()`
raised, the hook's catch-all swallowed it and exited 0, and telemetry for that
session was dead from that moment on. Repairing the file by hand made the very
next Stop emit 47 spans.

Sub-agents are what made this reachable: their start and stop hooks fire while
the parent is still calling tools, so several writers exist at once.
"""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from stdtel.state import SessionState


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    return tmp_path


def _torn(path: Path) -> None:
    """Exactly the shape the real corruption had: a whole document, then the
    tail of a longer one."""
    short = json.dumps({"transcript_offset": 4242, "started_at": 1.0, "resource": {},
                        "windows": [], "tool_calls": {}, "observed_events": []}, indent=1)
    longer = json.dumps({"transcript_offset": 9999, "subagents": [
        {"agent_id": "a", "agent_type": "Explore", "started_at": 1.0}]}, indent=1)
    path.write_text(short + longer[len(short):])


def test_a_torn_state_file_does_not_end_the_session(state_dir, capsys):
    """The failure this is all about: one bad write, and every later hook exits
    0 having done nothing, for the rest of the session's life."""
    _torn(state_dir / "s1.json")
    st = SessionState.load("s1")
    assert st.transcript_offset == 4242, "the intact leading document must be salvaged"
    assert "repaired" in capsys.readouterr().err, "a silent repair is its own bug"


def test_salvaging_keeps_the_offset_rather_than_rereading_everything(state_dir):
    """Starting over would reset `transcript_offset` to 0, re-read a transcript
    that can be 20 MB, and re-emit turns already recorded — turning one lost
    write into duplicate rows."""
    _torn(state_dir / "s2.json")
    assert SessionState.load("s2").transcript_offset > 0


def test_unsalvageable_state_starts_over_and_says_so(state_dir, capsys):
    (state_dir / "s3.json").write_text("}{ not json at all")
    st = SessionState.load("s3")
    assert st.transcript_offset == 0
    err = capsys.readouterr().err
    assert "starting this session's state over" in err
    assert "Spans already sent are unaffected" in err, "say what was and was not lost"


def test_a_write_is_atomic_so_a_reader_never_sees_half_of_one(state_dir):
    """`write_text` truncates and then writes. Renaming a finished temporary
    file means a reader gets the old state or the new one, never a blend."""
    st = SessionState(session_id="s4")
    st.transcript_offset = 7
    st.save()
    st.transcript_offset = 8
    st.save()
    assert json.loads((state_dir / "s4.json").read_text())["transcript_offset"] == 8
    assert not list(state_dir.glob(".s4.*.tmp")), "temporary files must not be left behind"


def _hammer(args):
    """One hook process: add a sub-agent, as SubagentStart does."""
    state_dir, session_id, n = args
    os.environ["STDTEL_STATE_DIR"] = str(state_dir)
    for i in range(12):
        with SessionState.mutate(session_id) as st:
            st.open_subagent(agent_id=f"a{n}-{i}", agent_type="Explore")
    return n


def test_parallel_hooks_neither_corrupt_nor_lose_state(state_dir):
    """The real scenario, with real processes.

    Without the lock this both tears the file and drops writes: each process
    reads, appends one sub-agent, and writes back a copy that never saw the
    others. A sub-agent lost here is a cost nobody ever accounts for.
    """
    SessionState(session_id="race").save()
    workers = 4
    with multiprocessing.Pool(workers) as pool:
        pool.map(_hammer, [(state_dir, "race", n) for n in range(workers)])

    raw = (state_dir / "race.json").read_text()
    json.loads(raw)                      # must parse: no torn write
    st = SessionState.load("race")
    assert len(st.subagents) == workers * 12, \
        f"lost {workers * 12 - len(st.subagents)} sub-agent(s) to concurrent writes"


def test_the_lock_file_is_not_mistaken_for_state(state_dir):
    """The lock lives beside the state; a stray `.lock` must not be loaded as a
    session, and the doctor counts `*.json`."""
    with SessionState.mutate("s5"):
        pass
    assert (state_dir / "s5.json").exists()
    locks = list(state_dir.glob("*.lock"))
    assert all(not p.name.endswith(".json") for p in locks)
