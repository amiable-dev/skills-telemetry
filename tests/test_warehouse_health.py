"""#152, ADR-015 decision 3: data freshness and loader liveness are two checks.

On 2026-10-09 the warehouse was a week behind while every panel looked merely
quiet: nothing had loaded since 2 October, because nothing was scheduled to.

`warehouse freshness` measures data: a loader's watermark (the newest source
timestamp any successful run loaded) against whether the source now holds
anything newer than that by more than twice the load interval. `loader liveness`
measures the scheduler: the latest attempted run, whatever its status. A recent
failure is reported even when an older success makes the data look fresh, and
a source outage never hides a failed attempt.
"""
from __future__ import annotations

import datetime as dt

import pytest

from stdtel import doctor
from stdtel import warehouse_health as wh
from stdtel.warehouse_health import Run

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)
INTERVAL = 900
H = dt.timedelta(hours=1)
M = dt.timedelta(minutes=1)


def runs(**by_loader):
    """loader -> (latest attempt, watermark, has a successful run)."""
    return {k: v for k, v in by_loader.items()}


def fresh_all(**over):
    base = {name: wh.LoaderState(latest=Run(NOW - 5 * M, True, None), watermark=NOW - 10 * M, any_ok=True)
            for name in wh.LOADERS}
    base.update(over)
    return base


def liveness(state, now=NOW):
    return wh.judge_liveness(state, now=now, interval_s=INTERVAL)


def freshness(state, newer, now=NOW):
    return wh.judge_freshness(state, newer=newer, now=now, interval_s=INTERVAL)


NONE_NEWER = {name: False for name in wh.SOURCES}


# --- the eight W3 cases -----------------------------------------------------------------------

def test_fresh_data_with_a_recent_run():
    assert freshness(fresh_all(), NONE_NEWER).outcome == "pass"
    assert liveness(fresh_all()).outcome == "pass"


def test_a_quiet_source_with_an_old_complete_warehouse_is_fresh_but_the_loader_is_not_live():
    old = wh.LoaderState(latest=Run(NOW - 3 * 24 * H, True, None), watermark=NOW - 3 * 24 * H, any_ok=True)
    state = fresh_all(load_traces=old)
    assert freshness(state, NONE_NEWER).outcome == "pass"
    c = liveness(state)
    assert c.outcome == "fail" and "load_traces" in c.detail


def test_a_successful_run_that_loaded_nothing_while_the_source_moved_on_is_stale():
    """The run is ok and recent, but the watermark it left is old and the source has newer events."""
    state = fresh_all(load_requests=wh.LoaderState(latest=Run(NOW - 2 * M, True, None),
                                                   watermark=NOW - 5 * H, any_ok=True))
    c = freshness(state, {**NONE_NEWER, "load_requests": True})
    assert c.outcome == "fail" and "load_requests" in c.detail and c.remedy


def test_a_recent_partial_run_after_an_older_success_is_not_live_and_names_its_error():
    state = fresh_all(load_traces=wh.LoaderState(latest=Run(NOW - 2 * M, False, "HTTPError: 400 search window"),
                                                 watermark=NOW - 10 * M, any_ok=True))
    c = liveness(state)
    assert c.outcome == "fail" and "load_traces" in c.detail and "HTTPError: 400 search window" in c.detail


def test_no_attempt_within_twice_the_interval_is_not_live():
    stale = wh.LoaderState(latest=Run(NOW - 2 * INTERVAL * dt.timedelta(seconds=1) - M, True, None),
                           watermark=NOW - 10 * M, any_ok=True)
    assert liveness(fresh_all(load_requests=stale)).outcome == "fail"
    just = wh.LoaderState(latest=Run(NOW - 2 * INTERVAL * dt.timedelta(seconds=1) + M, True, None),
                          watermark=NOW - 10 * M, any_ok=True)
    assert liveness(fresh_all(load_requests=just)).outcome == "pass"


def test_a_loader_that_never_ran_is_not_live():
    c = liveness(fresh_all(load_delivery=wh.LoaderState(latest=None, watermark=None, any_ok=False)))
    assert c.outcome == "fail" and "load_delivery" in c.detail and "never" in c.detail


def test_postgres_down_is_unknown_for_both():
    def down():
        raise ConnectionRefusedError("nothing on 5432")
    f = wh.warehouse_freshness(read_state=down, newer=lambda loader, since: False, now=NOW)
    l_ = wh.loader_liveness(read_state=down, now=NOW)
    assert (f.outcome, l_.outcome) == ("unknown", "unknown")
    assert "Postgres" in f.detail and "Postgres" in l_.detail and f.remedy and l_.remedy


def test_source_unreadable_with_postgres_up_and_a_failed_attempt():
    """Freshness is unknown; liveness, which needs only Postgres, still fails and names the error."""
    state = fresh_all(load_requests=wh.LoaderState(latest=Run(NOW - 2 * M, False, "ConnectError: loki"),
                                                   watermark=NOW - 10 * M, any_ok=True))

    def newer(loader, since):
        raise ConnectionRefusedError("Loki down")
    f = wh.warehouse_freshness(read_state=lambda: state, newer=newer, now=NOW)
    l_ = wh.loader_liveness(read_state=lambda: state, now=NOW)
    assert f.outcome == "unknown" and "load_requests" in f.detail
    assert l_.outcome == "fail" and "ConnectError: loki" in l_.detail


def test_a_loader_with_no_successful_run_has_no_watermark_so_freshness_is_unknown():
    state = fresh_all(load_traces=wh.LoaderState(latest=Run(NOW - 2 * M, False, "boom"), watermark=None,
                                                 any_ok=False))
    c = freshness(state, NONE_NEWER)
    assert c.outcome == "unknown" and "load_traces" in c.detail and c.remedy


# --- the edges --------------------------------------------------------------------------------

def test_the_source_is_asked_about_events_past_the_allowance():
    """Newer than the watermark by more than twice the interval, not merely newer."""
    asked = {}

    def newer(loader, since):
        asked[loader] = since
        return False
    state = fresh_all()
    wh.warehouse_freshness(read_state=lambda: state, newer=newer, now=NOW, interval_s=INTERVAL)
    assert set(asked) == set(wh.SOURCES)
    assert asked["load_traces"] == state["load_traces"].watermark + dt.timedelta(seconds=2 * INTERVAL)


def test_ok_runs_that_never_loaded_a_row_have_no_watermark_but_are_judged():
    """There was a success; it just carried nothing. Any source event in reach is then too new."""
    state = fresh_all(load_requests=wh.LoaderState(latest=Run(NOW - M, True, None), watermark=None, any_ok=True))
    asked = {}

    def newer(loader, since):
        asked[loader] = since
        return loader == "load_requests"
    c = wh.warehouse_freshness(read_state=lambda: state, newer=newer, now=NOW, interval_s=INTERVAL)
    assert c.outcome == "fail" and asked["load_requests"] is None


def test_delivery_is_judged_for_liveness_only():
    """Its source is GitHub, which doctor does not read."""
    assert "load_delivery" in wh.LOADERS and "load_delivery" not in wh.SOURCES


def test_precedence_fail_over_unknown():
    state = fresh_all(load_traces=wh.LoaderState(latest=Run(NOW - M, False, "x"), watermark=None, any_ok=False))
    c = freshness(state, {**NONE_NEWER, "load_requests": True})
    assert c.outcome == "fail"


def test_an_error_is_shown_bounded():
    state = fresh_all(load_traces=wh.LoaderState(latest=Run(NOW - M, False, "E" * 5000), watermark=NOW, any_ok=True))
    assert len(liveness(state).detail) < 1000


def test_the_interval_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("STDTEL_LOAD_INTERVAL", "60")
    assert wh.load_interval_s() == 60
    monkeypatch.delenv("STDTEL_LOAD_INTERVAL")
    assert wh.load_interval_s() == 900


@pytest.mark.parametrize("bad", ["0", "-5", "soon"])
def test_a_bad_interval_is_unknown_not_a_crash(monkeypatch, bad):
    monkeypatch.setenv("STDTEL_LOAD_INTERVAL", bad)
    c = wh.loader_liveness(read_state=lambda: fresh_all(), now=NOW)
    assert c.outcome == "unknown" and "STDTEL_LOAD_INTERVAL" in c.detail


def test_nothing_in_either_check_raises(monkeypatch):
    monkeypatch.setattr(wh, "judge_liveness", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(wh, "judge_freshness", lambda *a, **k: 1 / 0)
    assert wh.loader_liveness(read_state=lambda: fresh_all(), now=NOW).outcome == "unknown"
    assert wh.warehouse_freshness(read_state=lambda: fresh_all(), newer=lambda l, s: False, now=NOW).outcome == "unknown"


def test_without_the_warehouse_extras_both_are_unknown_with_the_install_remedy(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_psycopg(name, *a, **k):
        if name == "psycopg":
            raise ImportError("No module named 'psycopg'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    c = wh.loader_liveness(now=NOW)
    assert c.outcome == "unknown" and "stdtel[warehouse]" in c.remedy
    assert "not installed" in c.detail, "a missing extra is named, not reported as Postgres being down"


def test_the_dsn_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("STDTEL_DSN", "postgresql://elsewhere/db")
    assert wh._dsn() == "postgresql://elsewhere/db"
    monkeypatch.delenv("STDTEL_DSN")
    assert wh._dsn() == wh.DEFAULT_DSN


# --- the source queries mirror the loaders' ---------------------------------------------------

def test_the_queries_are_the_loaders_own():
    from warehouse import load_requests, load_traces
    assert wh.LOKI_QUERY == load_requests.QUERY
    assert set(wh.TEMPO_SPAN_NAMES) == {load_traces.SPAN_NAME, load_traces.LEGACY_SKILL_SPAN_NAME,
                                        load_traces.SESSION_SPAN_NAME}
    assert set(wh.LOADERS) == {load_traces.LOADER, load_requests.LOADER, "load_delivery"}


def test_tempo_newer_is_one_search_clamped_to_its_maximum_window():
    seen = []

    def get(url):
        seen.append(url)
        return {"traces": [{"traceID": "a"}]}
    since = NOW - 30 * 24 * H
    assert wh.tempo_has_newer(since, now=NOW, get=get) is True
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert int(q["end"][0]) - int(q["start"][0]) <= 168 * 3600
    assert "std.artefact.activation" in q["q"][0] and q["limit"] == ["1"]


def test_loki_newer_counts_from_since():
    seen = []

    def get(url):
        seen.append(url)
        return {"status": "success", "data": {"result": [{"values": [["1", "x"]]}]}}
    assert wh.loki_has_newer(NOW - H, now=NOW, get=get) is True
    assert wh.loki_has_newer(NOW - H, now=NOW, get=lambda u: {"status": "success", "data": {"result": []}}) is False
    with pytest.raises(RuntimeError):
        wh.loki_has_newer(NOW - H, now=NOW, get=lambda u: {"status": "error"})


# --- wiring -----------------------------------------------------------------------------------

def test_both_are_doctors_checks():
    assert doctor.warehouse_freshness in doctor.CHECKS and doctor.loader_liveness in doctor.CHECKS
    assert doctor.warehouse_freshness.check_name == "warehouse freshness"
    assert doctor.loader_liveness.check_name == "loader liveness"


# --- reading loader_run (real Postgres, wtest- rows only) -------------------------------------

DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(DSN, connect_timeout=2, autocommit=True)
    except Exception:                                          # noqa: BLE001
        pytest.skip("no local warehouse")
    clean = lambda: conn.execute("DELETE FROM loader_run WHERE run_id LIKE 'wtest-%'")   # noqa: E731
    clean()
    yield conn
    clean()
    conn.close()


def test_read_state_takes_the_latest_attempt_and_the_highest_ok_watermark(pg):
    rows = [  # run_id, started, loader, ok, source_max_ts, error
        ("wtest-1", NOW - 3 * H, "wtest-a", True, NOW - 4 * H, None),
        ("wtest-2", NOW - 2 * H, "wtest-a", True, None, None),            # ok, loaded nothing
        ("wtest-3", NOW - 1 * H, "wtest-a", False, NOW - 30 * M, "boom"),  # partial: never a watermark
        ("wtest-4", NOW - 1 * H, "wtest-b", False, None, "never ok"),
    ]
    for r in rows:
        pg.execute("INSERT INTO loader_run (run_id, started_at, loader, ok, source_max_ts, error) "
                   "VALUES (%s, %s, %s, %s, %s, %s)", r)
    got = wh.read_state(DSN, loaders=("wtest-a", "wtest-b", "wtest-c"))
    assert got["wtest-a"] == wh.LoaderState(Run(NOW - H, False, "boom"), NOW - 4 * H, True)
    assert got["wtest-b"] == wh.LoaderState(Run(NOW - H, False, "never ok"), None, False)
    assert got["wtest-c"] == wh.LoaderState(None, None, False)
