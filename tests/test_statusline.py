"""The statusline: data-quality faults, while they are still fixable.

An unattributed branch renamed tomorrow does not retroactively attribute today's
PRs. Loading this repository's first ten merged PRs produced zero ticket rows for
exactly that reason. The statusline is how that reaches the one person who can
fix it, at the only time they can.

It must be fast and local: Claude Code debounces at 300ms and cancels an
in-flight script when a new update arrives.
"""
import json
import os
import time
from pathlib import Path

import pytest

from stdtel.statusline import main, render

SESSION = {"session_id": "sl-test", "workspace": {"current_dir": "/tmp"},
           "model": {"display_name": "Opus"},
           "cost": {"total_cost_usd": 4.20, "total_duration_ms": 90000}}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("STDTEL_DISABLED", raising=False)
    monkeypatch.delenv("STDTEL_STATUSLINE", raising=False)

    def write(resource=None, **extra):
        from stdtel.state import SessionState
        st = SessionState.load("sl-test")
        st.resource = resource if resource is not None else {"std.ticket.id": "PLAT-42"}
        for k, v in extra.items():
            setattr(st, k, v)
        st.save()
        return st
    return write


# --- the fault it exists to surface ---

def test_flags_a_branch_with_no_ticket_key(state):
    state({"std.ticket.id": "unattributed"})
    out = render(SESSION)
    assert "no ticket" in out.lower()


def test_silent_about_attribution_when_the_branch_is_fine(state):
    state({"std.ticket.id": "PLAT-42"})
    out = render(SESSION)
    assert "no ticket" not in out.lower()
    assert "PLAT-42" in out


def test_reports_uncatalogued_skills(state, monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path / "empty"))
    st = state()
    st.open_window("some-unknown-skill", "unversioned", "direct", "t1")
    st.save()
    assert "unversioned" in render(SESSION).lower()


def test_reports_a_failed_export(state):
    state(last_export_ok=False)
    out = render(SESSION).lower()
    assert "drop" in out or "collector" in out


# --- what it must never show ---

def test_never_shows_per_developer_cost_or_tokens(state):
    """docs/for-developers.md commits to per-skill analysis, not individual
    measurement. A live running total of your own spend in your editor reads as
    surveillance however it is framed — and the payload offers it."""
    state()
    out = render(SESSION)
    assert "4.2" not in out and "$" not in out
    for banned in ("token", "cost", "usd"):
        assert banned not in out.lower(), f"statusline must not show {banned}"


# --- it must get out of the way ---

def test_quiet_when_everything_is_fine(state):
    state({"std.ticket.id": "PLAT-42"})
    out = render(SESSION)
    assert len(out) < 40, f"noise in a statusline gets the whole thing removed: {out!r}"
    assert "⚠" not in out


def test_disabled_renders_nothing(state, monkeypatch):
    state({"std.ticket.id": "unattributed"})
    monkeypatch.setenv("STDTEL_DISABLED", "1")
    assert render(SESSION) == ""


def test_statusline_can_be_turned_off_independently(state, monkeypatch):
    """Some people want telemetry without the indicator."""
    state({"std.ticket.id": "unattributed"})
    monkeypatch.setenv("STDTEL_STATUSLINE", "off")
    assert render(SESSION) == ""


def test_renders_without_any_session_state(state):
    """First render of a brand new session, before any hook has written state."""
    assert isinstance(render({"session_id": "never-seen"}), str)


def test_never_raises_on_a_malformed_payload(state):
    for payload in ({}, {"session_id": None}, {"workspace": None}):
        assert isinstance(render(payload), str)


def test_is_fast(state):
    """Cancelled if a new update arrives; a slow statusline never renders."""
    state()
    t0 = time.perf_counter()
    for _ in range(20):
        render(SESSION)
    per_call = (time.perf_counter() - t0) / 20
    assert per_call < 0.02, f"{per_call*1000:.1f}ms per render is too slow"


def test_makes_no_network_call(state, monkeypatch):
    """It renders on every keystroke-ish event; it must not touch the collector."""
    import socket
    def boom(*a, **k):
        raise AssertionError("statusline attempted a network connection")
    monkeypatch.setattr(socket.socket, "connect", boom)
    state()
    render(SESSION)


def test_cli_reads_stdin_and_always_exits_zero(state, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SESSION)))
    assert main([]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main([]) == 0, "a broken payload must not break the status bar"
