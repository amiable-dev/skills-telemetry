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
                                 for table, _, rows, _ in batches if rows})
        return sum(len(rows) for _, _, rows, _ in batches)
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
