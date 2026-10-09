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
    assert f.outcome == "unknown" and "load_requests" in f.detail and "Loki unreadable" in f.detail
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



# --- council round 1 on #169: a negative answer must be one the source could give -----------

def _never(url):
    raise AssertionError(f"queried a source for the future: {url}")


@pytest.mark.parametrize("has_newer", [wh.tempo_has_newer, wh.loki_has_newer])
def test_a_cutoff_in_the_future_is_answered_without_a_query(has_newer):
    """A healthy recent watermark plus the allowance lies past now: nothing can be newer."""
    assert has_newer(NOW + 5 * M, now=NOW, get=_never) is False


def test_an_empty_tempo_search_over_a_clamped_window_cannot_tell():
    """Tempo searches at most 168 h. Nothing found in the last 168 h says nothing
    about the stretch between an older cutoff and the window."""
    empty = lambda url: {"traces": []}                     # noqa: E731
    found = lambda url: {"traces": [{"traceID": "a"}]}     # noqa: E731
    assert wh.tempo_has_newer(NOW - 30 * 24 * H, now=NOW, get=empty) is None
    assert wh.tempo_has_newer(NOW - 30 * 24 * H, now=NOW, get=found) is True
    assert wh.tempo_has_newer(NOW - H, now=NOW, get=empty) is False
    assert wh.tempo_has_newer(None, now=NOW, get=empty) is None


def test_an_empty_loki_query_over_a_clamped_window_cannot_tell():
    empty = lambda url: {"status": "success", "data": {"result": []}}   # noqa: E731
    assert wh.loki_has_newer(NOW - 30 * 24 * H, now=NOW, get=empty) is None
    assert wh.loki_has_newer(None, now=NOW, get=empty) is None
    assert wh.loki_has_newer(NOW - H, now=NOW, get=empty) is False


def test_cannot_tell_is_unknown():
    c = freshness(fresh_all(), {**NONE_NEWER, "load_traces": None})
    assert c.outcome == "unknown" and "load_traces" in c.detail


@pytest.mark.parametrize("answer", [None, "yes", 0, 1, object()])
def test_only_an_exact_bool_is_an_answer(answer):
    """Truthiness would read a missing or malformed answer as "nothing newer", a false pass."""
    c = freshness(fresh_all(), {**NONE_NEWER, "load_requests": answer})
    assert c.outcome == "unknown"


def test_a_missing_answer_is_unknown_not_a_pass():
    c = freshness(fresh_all(), {"load_traces": False})
    assert c.outcome == "unknown" and "load_requests" in c.detail


@pytest.mark.parametrize("body", [{}, {"traces": None}, {"error": "bad query"}, []])
def test_a_tempo_response_without_a_trace_list_raises(body):
    with pytest.raises(RuntimeError):
        wh.tempo_has_newer(NOW - H, now=NOW, get=lambda url: body)


def test_the_real_url_builders_never_send_start_after_end():
    import urllib.parse
    for since in (NOW - H, NOW - 30 * 24 * H, None):
        for has_newer in (wh.tempo_has_newer, wh.loki_has_newer):
            seen = []

            def get(url):
                seen.append(url)
                return {"traces": [], "status": "success", "data": {"result": []}}
            has_newer(since, now=NOW, get=get)
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
            assert int(q["start"][0]) < int(q["end"][0])


def test_loki_start_is_exact_to_the_nanosecond():
    seen = []
    since = dt.datetime(2026, 10, 9, 11, 0, 0, 123456, tzinfo=dt.timezone.utc)
    wh.loki_has_newer(since, now=NOW, get=lambda u: seen.append(u) or {"status": "success", "data": {"result": []}})
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert int(q["start"][0]) == 1791543600_123456000 + 1


@pytest.mark.parametrize("bad", [0, -60])
def test_an_explicit_bad_interval_is_unknown(bad):
    c = wh.warehouse_freshness(read_state=lambda: fresh_all(), newer=lambda l, s: False, now=NOW, interval_s=bad)
    assert c.outcome == "unknown"


def test_ties_on_start_time_resolve_by_run_id(pg):
    for rid, ok in (("wtest-t1", True), ("wtest-t2", False)):
        pg.execute("INSERT INTO loader_run (run_id, started_at, loader, ok) VALUES (%s, %s, 'wtest-tie', %s)",
                   (rid, NOW, ok))
    assert wh.read_state(DSN, loaders=("wtest-tie",))["wtest-tie"].latest.ok is False


def test_a_loki_stream_with_no_values_is_not_an_event():
    body = {"status": "success", "data": {"result": [{"stream": {"service_name": "claude-code"}, "values": []}]}}
    assert wh.loki_has_newer(NOW - H, now=NOW, get=lambda u: body) is False


def _tempo_bounds(since, now):
    import urllib.parse
    seen = []
    wh.tempo_has_newer(since, now=now, get=lambda u: seen.append(u) or {"traces": []})
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    return int(q["start"][0]), int(q["end"][0])


FRACTIONAL = NOW + dt.timedelta(milliseconds=700)


@pytest.mark.parametrize("since", [FRACTIONAL - 30 * 24 * H, None])
def test_a_clamped_tempo_window_never_exceeds_the_maximum_with_fractional_seconds(since):
    """#169 round 2: start floored and end ceiled made it 168 h plus a second."""
    start, end = _tempo_bounds(since, FRACTIONAL)
    assert end - start <= wh.TEMPO_MAX_WINDOW_S


def test_tempo_rounds_inward_so_nothing_before_the_cutoff_counts():
    """The start rounds up and the end down: never an event before the cutoff,
    at the cost of under a second added to a 30-minute allowance."""
    since = NOW - H + dt.timedelta(milliseconds=500)
    start, end = _tempo_bounds(since, FRACTIONAL)
    assert start == int((NOW - H).timestamp()) + 1
    assert end == int(NOW.timestamp())


def test_a_cutoff_inside_the_current_second_is_answered_without_a_query():
    """Rounded inward, the window would be empty or inverted: nothing can be newer."""
    assert wh.tempo_has_newer(FRACTIONAL - dt.timedelta(milliseconds=200), now=FRACTIONAL, get=_never) is False


@pytest.mark.parametrize("body", [{"status": "success"}, {"status": "success", "data": {}},
                                  {"status": "success", "data": {"result": None}}])
def test_a_malformed_loki_success_raises_a_protocol_error(body):
    with pytest.raises(RuntimeError):
        wh.loki_has_newer(NOW - H, now=NOW, get=lambda u: body)


def test_a_huge_interval_is_unknown_not_an_overflow(monkeypatch):
    monkeypatch.setenv("STDTEL_LOAD_INTERVAL", str(10**18))
    c = wh.loader_liveness(read_state=lambda: fresh_all(), now=NOW)
    assert c.outcome == "unknown" and "STDTEL_LOAD_INTERVAL" in c.detail


def test_a_loader_error_is_shown_without_control_characters():
    state = fresh_all(load_traces=wh.LoaderState(latest=Run(NOW - M, False, "bad\x1b[31m\nnext\x07"),
                                                 watermark=NOW, any_ok=True))
    d = liveness(state).detail
    assert "\x1b" not in d and "\x07" not in d and "\n" not in d and "bad" in d


def test_a_loader_missing_from_the_state_is_treated_as_never_run():
    state = fresh_all()
    del state["load_delivery"]
    c = liveness(state)
    assert c.outcome == "fail" and "load_delivery has never run" in c.detail


def test_a_loki_window_is_clamped_to_its_own_maximum():
    import urllib.parse
    seen = []
    wh.loki_has_newer(NOW - 30 * 24 * H, now=NOW, get=lambda u: seen.append(u) or {"status": "success", "data": {"result": []}})
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert int(q["end"][0]) - int(q["start"][0]) <= wh.LOKI_WINDOW_S * 10**9


def test_a_loki_error_status_raises_even_with_a_result():
    with pytest.raises(RuntimeError, match="failed"):
        wh.loki_has_newer(NOW - H, now=NOW, get=lambda u: {"status": "error", "data": {"result": []}})
