"""#170, ADR-015 decision 5: `stdtel-load` runs every loader once, under a lock.

Nothing loaded the warehouse on a schedule, so on 2026-10-09 it was a week
behind. A scheduler (#171) needs one command to run: this one. It must never
run twice at once, never leave a lock that blocks it for ever, keep its log
bounded, and reach back far enough to repair an outage longer than a day,
which a fixed `--since 24h` cannot and #152's freshness check would not see.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

import pytest

from stdtel import load

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_HOME", str(tmp_path))
    return tmp_path


# --- the lock -------------------------------------------------------------------------------

def test_the_lock_is_taken_and_released(home):
    with load.lock() as held:
        assert held and (home / "loader.lock").is_dir()
        owner = json.loads((home / "loader.lock" / "owner").read_text())
        assert owner["pid"] == os.getpid() and owner["start"] == load.process_start(os.getpid())
    assert not (home / "loader.lock").exists()


def test_an_overlapping_run_exits_under_the_lock(home):
    with load.lock() as first:
        with load.lock() as second:
            assert first and not second
    assert not (home / "loader.lock").exists()


def _plant(home, pid, start):
    d = home / "loader.lock"
    d.mkdir()
    (d / "owner").write_text(json.dumps({"pid": pid, "start": start}))


def test_a_lock_whose_pid_is_dead_is_reclaimed(home):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    _plant(home, p.pid, "Thu Jan  1 00:00:00 1970")
    with load.lock() as held:
        assert held


def test_a_lock_whose_pid_is_alive_is_never_reclaimed(home):
    """However old: a long run is never doubled."""
    _plant(home, os.getpid(), load.process_start(os.getpid()))
    old = (home / "loader.lock").stat().st_mtime - 10 * 24 * 3600
    os.utime(home / "loader.lock", (old, old))
    with load.lock() as held:
        assert not held
    assert (home / "loader.lock").exists(), "someone else's live lock is left alone"


def test_a_live_pid_with_a_different_start_time_is_a_reused_pid_and_is_reclaimed(home):
    _plant(home, os.getpid(), "Thu Jan  1 00:00:00 1970")
    with load.lock() as held:
        assert held


def test_an_unreadable_owner_is_treated_as_stale(home):
    (home / "loader.lock").mkdir()
    (home / "loader.lock" / "owner").write_text("{ half")
    with load.lock() as held:
        assert held


def test_process_start_is_stable_and_differs_for_another_process():
    me = load.process_start(os.getpid())
    assert me and me == load.process_start(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert load.process_start(p.pid) not in (None, "")
    finally:
        p.kill()
        p.wait()
    assert load.process_start(p.pid) is None, "a dead process has no start time"


def test_a_skipped_run_writes_nothing(home, monkeypatch):
    ran = []
    monkeypatch.setattr(load, "run_all", lambda **kw: ran.append(1) or 0)
    with load.lock():
        assert load.main([]) == 0
    assert ran == [], "an overlapping run neither loads nor records a loader_run row"


# --- the log ---------------------------------------------------------------------------------

def test_the_log_is_kept_to_its_last_megabyte(home):
    log = home / "loader.log"
    log.write_bytes(b"old line\n" * 200_000 + b"newest line\n")      # ~1.8 MB
    load.trim_log(log)
    data = log.read_bytes()
    assert len(data) <= load.LOG_MAX_BYTES
    assert data.endswith(b"newest line\n") and data.startswith(b"old line\n"), "cut at a line boundary"


def test_a_short_log_is_left_alone(home):
    log = home / "loader.log"
    log.write_text("one\n")
    load.trim_log(log)
    assert log.read_text() == "one\n"


def test_main_writes_to_the_log(home, monkeypatch):
    def run_all(**kw):
        print("loaded 3 activation(s)")
        return 0
    monkeypatch.setattr(load, "run_all", run_all)
    assert load.main([]) == 0
    assert "loaded 3 activation(s)" in (home / "loader.log").read_text()


# --- the window ------------------------------------------------------------------------------

@pytest.mark.parametrize("watermark,want", [
    (None, 24),                         # never loaded: the floor
    (NOW - 3 * H, 24),                  # recent: the floor
    (NOW - 7 * 24 * H, 7 * 24 + 2),     # a week behind: reach back to it, plus the overlap
    (NOW - 90 * 24 * H, 720),           # older than any source keeps: the cap
])
def test_the_window_reaches_back_to_the_watermark(watermark, want):
    assert load.window_hours(watermark, now=NOW) == want


def test_a_partial_hour_rounds_up():
    assert load.window_hours(NOW - (100 * H + dt.timedelta(minutes=1)), now=NOW) == 103


# --- the run ---------------------------------------------------------------------------------

def test_every_loader_runs_in_order_with_its_window(monkeypatch):
    calls = []
    monkeypatch.setattr(load, "watermarks", lambda dsn: {"load_traces": NOW - 7 * 24 * H, "load_requests": None})
    monkeypatch.setattr(load, "_run", lambda name, argv: calls.append((name, argv)) or 0)
    monkeypatch.setenv("STDTEL_LOAD_REPO", "a/b")
    monkeypatch.setattr(load, "_now", lambda: NOW)
    assert load.run_all(dsn="postgresql://x", tempo="http://t", loki="http://l") == 0
    assert [c[0] for c in calls] == ["load_traces", "load_requests", "fetch_policy_results", "load_delivery"]
    assert calls[0][1][-2:] == ["--since", "170h"] and calls[1][1][-2:] == ["--since", "24h"]


def test_one_loader_failing_does_not_stop_the_others(monkeypatch):
    calls = []
    monkeypatch.setattr(load, "watermarks", lambda dsn: {})
    monkeypatch.setattr(load, "_run", lambda name, argv: calls.append(name) or (1 if name == "load_traces" else 0))
    monkeypatch.setenv("STDTEL_LOAD_REPO", "a/b")
    assert load.run_all(dsn="d", tempo="t", loki="l") == 1
    assert "load_requests" in calls and "load_delivery" in calls


def test_without_a_repo_delivery_records_a_failed_run_naming_the_setting(monkeypatch):
    """Loud and truthful: `loader liveness` names the one thing to set, rather than
    reporting a loader that silently never runs."""
    recorded = []
    monkeypatch.delenv("STDTEL_LOAD_REPO", raising=False)
    monkeypatch.setattr(load, "watermarks", lambda dsn: {})
    monkeypatch.setattr(load, "_run", lambda name, argv: 0)
    monkeypatch.setattr(load, "_record", lambda dsn, row: recorded.append(row) or True)
    load.run_all(dsn="d", tempo="t", loki="l")
    (row,) = recorded
    assert row["loader"] == "load_delivery" and row["ok"] is False and "STDTEL_LOAD_REPO" in row["error"]


def test_a_failing_policy_fetch_still_loads_delivery(monkeypatch):
    """As `make load-delivery` does: the artefact is enrichment, not a precondition."""
    calls = []
    monkeypatch.setattr(load, "watermarks", lambda dsn: {})
    monkeypatch.setattr(load, "_run", lambda name, argv: calls.append(name) or (1 if name == "fetch_policy_results" else 0))
    monkeypatch.setenv("STDTEL_LOAD_REPO", "a/b")
    assert load.run_all(dsn="d", tempo="t", loki="l") == 0
    assert calls[-1] == "load_delivery"


def test_unreadable_watermarks_fall_back_to_the_floor(monkeypatch):
    calls = []

    def down(dsn):
        raise ConnectionError("no postgres")
    monkeypatch.setattr(load, "watermarks", down)
    monkeypatch.setattr(load, "_run", lambda name, argv: calls.append(argv) or 0)
    monkeypatch.setenv("STDTEL_LOAD_REPO", "a/b")
    load.run_all(dsn="d", tempo="t", loki="l")
    assert calls[0][-2:] == ["--since", "24h"]


# --- packaging -------------------------------------------------------------------------------

def test_the_loaders_ship_in_the_wheel_and_the_command_exists():
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cfg = tomllib.loads((root / "pyproject.toml").read_text())
    assert "warehouse*" in cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    assert (root / "warehouse" / "__init__.py").exists(), "packages.find skips a namespace package"
    assert cfg["project"]["scripts"]["stdtel-load"] == "stdtel.load:main"


def test_a_lock_taken_over_meanwhile_is_not_released_by_the_old_owner(home):
    """If this run's lock was reclaimed and another run holds it now, leaving
    must not delete that run's lock."""
    with load.lock() as held:
        assert held
        (home / "loader.lock" / "owner").write_text(json.dumps({"pid": 1, "start": "someone else"}))
    assert (home / "loader.lock").exists()


def test_the_log_bound_is_one_megabyte():
    assert load.LOG_MAX_BYTES == 1024 * 1024


def test_a_failing_delivery_load_fails_the_run(monkeypatch):
    monkeypatch.setattr(load, "watermarks", lambda dsn: {})
    monkeypatch.setattr(load, "_run", lambda name, argv: 1 if name == "load_delivery" else 0)
    monkeypatch.setenv("STDTEL_LOAD_REPO", "a/b")
    assert load.run_all(dsn="d", tempo="t", loki="l") == 1
