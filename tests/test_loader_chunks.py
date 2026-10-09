"""#153, ADR-015 decision 5: a load window wider than Tempo's search maximum is
split into half-open chunks, and a partial load is a failed load.

On 2026-10-09 a 192-hour backfill got HTTP 400 from Tempo, whose search refuses
any window wider than `query_frontend.search.max_duration` (168 h here). A
loader that has fallen a week behind could therefore never catch up.

Each chunk is written as it loads, so a failing chunk keeps the ones already
loaded; the run is then `ok = false`, naming the chunk, so it never advances
the freshness watermark (#152). There is no checkpoint: the rows are the
progress, and every insert is idempotent on its key.
"""
from __future__ import annotations

import datetime as dt

import pytest

from tests.test_warehouse_activation import otlp_span, trace_doc
from warehouse import load_traces as lt

H = 3600
T0 = int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())


# --- the chunks -------------------------------------------------------------------------------

def test_a_192_hour_window_splits_into_two_chunks():
    assert lt.chunks(T0, T0 + 192 * H, 168 * H) == [(T0, T0 + 168 * H), (T0 + 168 * H, T0 + 192 * H)]


def test_chunks_are_half_open_oldest_first_and_cover_the_window_exactly():
    got = lt.chunks(T0, T0 + 400 * H, 168 * H)
    assert got[0][0] == T0 and got[-1][1] == T0 + 400 * H
    assert all(a[1] == b[0] for a, b in zip(got, got[1:]))
    assert all(e - s <= 168 * H for s, e in got)


def test_a_window_within_the_maximum_is_one_chunk():
    assert lt.chunks(T0, T0 + 24 * H, 168 * H) == [(T0, T0 + 24 * H)]


def test_no_maximum_is_one_chunk():
    assert lt.chunks(T0, T0 + 900 * H, None) == [(T0, T0 + 900 * H)]


# --- Tempo's maximum ---------------------------------------------------------------------------

CONFIG = "query_frontend:\n    search:\n        max_duration: {}\n    metrics:\n        max_duration: 3h0m0s\n"


@pytest.mark.parametrize("text,want", [("168h0m0s", 168 * H), ("24h", 24 * H), ("0s", None)])
def test_the_maximum_is_read_from_tempos_search_config(text, want):
    assert lt.tempo_max_window_s("http://t", get_text=lambda url: CONFIG.format(text)) == want


@pytest.mark.parametrize("get_text", [lambda url: "not: [yaml", lambda url: "{}",
                                      lambda url: (_ for _ in ()).throw(ConnectionError())])
def test_an_unreadable_maximum_falls_back_to_168_hours(get_text):
    assert lt.tempo_max_window_s("http://t", get_text=get_text) == 168 * H


# --- loading chunk by chunk --------------------------------------------------------------------

def turn(span_id: str, at: int, trace: str) -> dict:
    s = otlp_span(lt.SPAN_NAME, span_id, {"session.id": "wtest-chunks", "std.artefact.kind": "turn",
                                           "std.artefact.source": "transcript",
                                           "std.prompt.id": "wtest-p-" + span_id},
                  start=dt.datetime.fromtimestamp(at, dt.timezone.utc))
    s["traceID"] = trace
    return s


#: trace id -> (its time, its span). "edge" sits on the chunk boundary, so a
#: search of either neighbouring chunk returns it.
TRACES = {
    "old": (T0 + 10 * H, turn("wtest-old", T0 + 10 * H, "old")),
    "edge": (T0 + 168 * H, turn("wtest-edge", T0 + 168 * H, "edge")),
    "mid": (T0 + 200 * H, turn("wtest-mid", T0 + 200 * H, "mid")),
    "new": (T0 + 350 * H, turn("wtest-new", T0 + 350 * H, "new")),
}


def fake(fail_chunk_containing: int | None = None):
    calls = {"search": [], "fetch": [], "written": []}

    def search(query, start, end):
        calls["search"].append((start, end))
        if fail_chunk_containing is not None and start <= fail_chunk_containing < end:
            raise RuntimeError("HTTP 500 from Tempo")
        if end - start > 168 * H:
            raise RuntimeError("HTTP 400: range exceeds max_duration")
        return [{"traceID": t} for t, (at, _) in TRACES.items() if start <= at <= end]   # inclusive: both see the edge

    def fetch(trace_id):
        calls["fetch"].append(trace_id)
        return trace_doc([TRACES[trace_id][1]])

    def write_chunk(batches):
        calls["written"].append({table: [r.get("span_id") or r.get("session_id") for r in rows]
                                 for table, _, rows, *_ in batches if rows})
        return sum(len(rows) for _, _, rows, *_ in batches)
    return search, fetch, write_chunk, calls


def span_ids_written(calls) -> list[str]:
    return [i for w in calls["written"] for i in w.get("artefact_activation", [])]


def test_every_chunk_loads_and_none_exceeds_the_maximum():
    search, fetch, write_chunk, calls = fake()
    r = lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert r.errors == []
    assert sorted(span_ids_written(calls)) == ["wtest-edge", "wtest-mid", "wtest-new", "wtest-old"]


def test_a_boundary_span_is_written_once():
    search, fetch, write_chunk, calls = fake()
    lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert span_ids_written(calls).count("wtest-edge") == 1
    assert calls["fetch"].count("edge") == 1, "a trace seen in two chunks is fetched once"


def test_a_failing_middle_chunk_fails_the_run_and_keeps_the_others():
    search, fetch, write_chunk, calls = fake(fail_chunk_containing=T0 + 200 * H)
    r = lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert len(r.errors) == 1 and "HTTP 500" in r.errors[0]
    assert str(T0 + 168 * H) in r.errors[0], "the error names the failing chunk"
    written = span_ids_written(calls)
    assert "wtest-old" in written and "wtest-new" in written and "wtest-mid" not in written
    assert r.activations == 3 and r.rows_loaded == 3, "a failed chunk adds nothing to the counts"


def test_each_chunk_is_written_as_it_loads():
    """One write per loaded chunk, so a later failure cannot take earlier rows with it."""
    search, fetch, write_chunk, calls = fake()
    lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert len(calls["written"]) == 3


def test_the_result_carries_what_the_run_row_needs():
    search, fetch, write_chunk, _ = fake()
    r = lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert r.rows_loaded == 4 and r.activations == 4
    assert r.source_max_ts == dt.datetime.fromtimestamp(T0 + 350 * H, dt.timezone.utc)


# --- main records a partial run as failed -------------------------------------------------------

def test_main_records_a_partial_run_as_not_ok(monkeypatch):
    recorded = {}
    search, fetch, write_chunk, _ = fake(fail_chunk_containing=T0 + 200 * H)
    monkeypatch.setattr(lt, "tempo_max_window_s", lambda base, get_text=None: 168 * H)
    monkeypatch.setattr(lt, "_tempo_io", lambda base: (search, fetch))
    monkeypatch.setattr(lt, "write", lambda dsn, batches: write_chunk(batches))
    monkeypatch.setattr(lt, "record_run", lambda dsn, row: recorded.update(row) or True)
    monkeypatch.setattr(lt, "_now_s", lambda: T0 + 400 * H)
    rc = lt.main(["--tempo", "http://t", "--dsn", "postgresql://x", "--since", "400h"])
    assert rc == 1 and recorded["ok"] is False and "HTTP 500" in recorded["error"]
    assert recorded["rows_loaded"] == 3, "the chunks that loaded are counted and kept"


def test_main_records_a_complete_run_as_ok(monkeypatch):
    recorded = {}
    search, fetch, write_chunk, _ = fake()
    monkeypatch.setattr(lt, "tempo_max_window_s", lambda base, get_text=None: 168 * H)
    monkeypatch.setattr(lt, "_tempo_io", lambda base: (search, fetch))
    monkeypatch.setattr(lt, "write", lambda dsn, batches: write_chunk(batches))
    monkeypatch.setattr(lt, "record_run", lambda dsn, row: recorded.update(row) or True)
    monkeypatch.setattr(lt, "_now_s", lambda: T0 + 400 * H)
    assert lt.main(["--tempo", "http://t", "--dsn", "postgresql://x", "--since", "400h"]) == 0
    assert recorded["ok"] is True and recorded["error"] is None and recorded["rows_loaded"] == 4


def test_the_newest_timestamp_is_the_newest_span_not_the_last_chunks(monkeypatch):
    """A sub-agent span carries its own, earlier start, so the last chunk can hold
    the oldest span. source_max_ts is the maximum over every chunk."""
    late_but_old = turn("wtest-late", T0 + 5 * H, "late")
    monkeypatch.delitem(TRACES, "new")              # the last chunk holds only the old-starting span
    monkeypatch.setitem(TRACES, "late", (T0 + 380 * H, late_but_old))
    search, fetch, write_chunk, _ = fake()
    r = lt.load_chunks(lt.chunks(T0, T0 + 400 * H, 168 * H), search, fetch, write_chunk)
    assert r.source_max_ts == dt.datetime.fromtimestamp(T0 + 200 * H, dt.timezone.utc)


# --- council round 1 on #172: nothing is lost silently ----------------------------------------

def test_a_search_that_hits_the_limit_splits_its_window_until_it_does_not():
    """Tempo returns at most `limit` traces and does not say there were more."""
    traces = {f"t{i}": T0 + i * 60 for i in range(25)}     # one a minute
    calls = []

    def search(query, start, end):
        calls.append((start, end))
        hits = [{"traceID": t} for t, at in traces.items() if start <= at < end]
        return hits[:10]                                      # a limit of 10
    got = lt.search_all(search, "q", T0, T0 + 25 * 60, limit=10)
    assert {t["traceID"] for t in got} == set(traces)
    assert all(e - s <= 25 * 60 for s, e in calls)


def test_a_window_still_full_at_the_smallest_split_fails_loudly():
    def search(query, start, end):
        return [{"traceID": f"t{start}-{i}"} for i in range(10)]   # always full
    with pytest.raises(RuntimeError, match="limit"):
        lt.search_all(search, "q", T0, T0 + 3600, limit=10)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_a_trace_fetch_that_errors_raises_rather_than_reading_as_empty(monkeypatch):
    import requests
    seen = {}

    def get(url, params=None, timeout=None, **kw):
        seen.setdefault("timeouts", []).append(timeout)
        if "/api/search" in url:
            return _Resp(200, {"traces": [{"traceID": "x"}]})
        return _Resp(500, {"error": "boom"})
    monkeypatch.setattr(requests, "get", get)
    search, fetch = lt._tempo_io("http://t/")
    assert search("q", T0, T0 + 60) == [{"traceID": "x"}]
    with pytest.raises(RuntimeError, match="HTTP 500"):
        fetch("x")
    assert all(t for t in seen["timeouts"]), "every Tempo call has a timeout"


def test_the_base_url_trailing_slash_is_normalised(monkeypatch):
    import requests
    urls = []
    monkeypatch.setattr(requests, "get", lambda url, **kw: urls.append(url) or _Resp(200, {"traces": []}))
    search, _ = lt._tempo_io("http://t/")
    search("q", T0, T0 + 60)
    assert urls == ["http://t/api/search"]


@pytest.mark.parametrize("since", ["2hh", "-5h", "0h", "24", "1d", "h"])
def test_a_malformed_since_is_a_usage_error(since):
    with pytest.raises(SystemExit):
        lt.parse_args(["--tempo", "http://t", "--dsn", "x", "--since", since])


def test_a_good_since_is_hours():
    assert lt.parse_args(["--tempo", "http://t", "--dsn", "x", "--since", "192h"]).since == "192h"


def test_the_maximum_falls_back_even_if_yaml_is_missing(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_yaml)
    assert lt.tempo_max_window_s("http://t", get_text=lambda u: "") == 168 * H


def test_load_chunks_searches_through_the_splitting_search(monkeypatch):
    """A chunk whose search fills the limit loses nothing: load_chunks splits it."""
    monkeypatch.setattr(lt, "SEARCH_LIMIT", 2)
    monkeypatch.setitem(TRACES, "extra", (T0 + 20 * H, turn("wtest-extra", T0 + 20 * H, "extra")))
    search, fetch, write_chunk, calls = fake()

    def capped(query, start, end):
        return search(query, start, end)[:2]
    lt.load_chunks([(T0, T0 + 168 * H), (T0 + 168 * H, T0 + 336 * H)], capped, fetch, write_chunk)
    assert sorted(span_ids_written(calls)) == ["wtest-edge", "wtest-extra", "wtest-mid", "wtest-old"]


# --- council round 2 on #172: the newest session snapshot wins --------------------------------

def session_span(sid: str, ended: int, tokens: int) -> dict:
    s = otlp_span(lt.SESSION_SPAN_NAME, "wtest-sc-" + str(ended), {
        "session.id": sid, "std.harness": "claude-code", "gen_ai.usage.input_tokens": tokens,
        "gen_ai.usage.output_tokens": 1}, start=dt.datetime.fromtimestamp(T0, dt.timezone.utc))
    s["endTimeUnixNano"] = str(ended * 10**9)
    return s


def parsed(span: dict) -> dict:
    (row,) = lt.collect({"t": {}}, lambda tid: trace_doc([span]))[2]
    return row


def test_a_session_snapshot_records_when_it_was_observed():
    """std.session.cost is emitted at every Stop with the transcript's running
    total; every snapshot starts at the session's start, and ends at its Stop."""
    row = parsed(session_span("wtest-s", T0 + 3 * H, 5))
    assert row["observed_at"] == dt.datetime.fromtimestamp(T0 + 3 * H, dt.timezone.utc)
    assert "observed_at" in lt.SESSION_COLS


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    dsn = "postgresql://postgres:stdtel@localhost:5432/stdtel"
    try:
        conn = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
    except Exception:                                      # noqa: BLE001
        pytest.skip("no local warehouse")
    from pathlib import Path
    conn.execute((Path(__file__).resolve().parent.parent / "warehouse" / "schema.sql").read_text())
    clean = lambda: conn.execute("DELETE FROM session_cost WHERE session_id LIKE 'wtest-%'")   # noqa: E731
    clean()
    yield dsn, conn
    clean()
    conn.close()


@pytest.mark.parametrize("order", [("old", "new"), ("new", "old")])
def test_the_newest_snapshot_wins_whatever_order_it_arrives_in(pg, order):
    dsn, conn = pg
    snaps = {"old": parsed(session_span("wtest-snap", T0 + 1 * H, 100)),
             "new": parsed(session_span("wtest-snap", T0 + 5 * H, 900))}
    for which in order:
        lt.write(dsn, [lt.session_batch([snaps[which]])])
    (tokens,) = conn.execute("SELECT input_tokens FROM session_cost WHERE session_id = 'wtest-snap'").fetchone()
    assert tokens == 900


def test_a_run_whose_row_cannot_be_recorded_does_not_exit_clean(monkeypatch):
    search, fetch, write_chunk, _ = fake()
    monkeypatch.setattr(lt, "tempo_max_window_s", lambda base, get_text=None: 168 * H)
    monkeypatch.setattr(lt, "_tempo_io", lambda base: (search, fetch))
    monkeypatch.setattr(lt, "write", lambda dsn, batches: write_chunk(batches))
    monkeypatch.setattr(lt, "record_run", lambda dsn, row: False)
    monkeypatch.setattr(lt, "_now_s", lambda: T0 + 400 * H)
    assert lt.main(["--tempo", "http://t", "--dsn", "postgresql://x", "--since", "400h"]) == 1


def test_within_one_batch_the_newest_snapshot_is_the_one_offered(pg):
    dsn, conn = pg
    rows = [parsed(session_span("wtest-batch", T0 + h * H, h * 100)) for h in (3, 7, 5)]
    lt.write(dsn, [lt.session_batch(rows)])
    (tokens,) = conn.execute("SELECT input_tokens FROM session_cost WHERE session_id = 'wtest-batch'").fetchone()
    assert tokens == 700


def test_a_row_written_before_observed_at_existed_is_replaced(pg):
    dsn, conn = pg
    conn.execute("INSERT INTO session_cost (session_id, input_tokens) VALUES ('wtest-legacy', 1)")
    lt.write(dsn, [lt.session_batch([parsed(session_span("wtest-legacy", T0 + H, 50))])])
    (tokens,) = conn.execute("SELECT input_tokens FROM session_cost WHERE session_id = 'wtest-legacy'").fetchone()
    assert tokens == 50


def test_load_chunks_writes_session_cost_newest_wins(monkeypatch):
    monkeypatch.setitem(TRACES, "sess", (T0 + 20 * H, {**session_span("wtest-sb", T0 + 21 * H, 5),
                                                       "traceID": "sess"}))
    seen = []
    search, fetch, _, _ = fake()
    lt.load_chunks([(T0, T0 + 168 * H)], search, fetch, lambda batches: seen.extend(batches) or 0)
    (batch,) = [b for b in seen if b[0] == "session_cost"]
    assert batch == lt.session_batch(batch[2]) and len(batch[2]) == 1
