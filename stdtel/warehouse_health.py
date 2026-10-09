"""`warehouse freshness` and `loader liveness` (ADR-015 decision 3).

Two checks, because they fail for different reasons and one can hide the other:

* **freshness** measures data. A loader's watermark is the newest source
  timestamp any successful run loaded (`max(source_max_ts) WHERE ok`), so a
  partial run never advances it. It is stale when its source now holds an
  event newer than the watermark by more than twice the load interval. Asking
  the source "anything past this point?" rather than "what is your newest?"
  needs no ordering guarantee from Tempo's search.
* **liveness** measures the scheduler: the latest attempted run per loader,
  whatever its status. It needs only Postgres, so a source outage never hides a
  failed attempt.

`load_delivery`'s source is GitHub, which doctor does not read: it is judged
for liveness only. psycopg is an optional extra (`stdtel[warehouse]`); without
it both checks are unknown and say how to install it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass

from stdtel.doctor import FAIL, PASS, UNKNOWN, Check

FRESHNESS, LIVENESS = "warehouse freshness", "loader liveness"
LOADERS = ("load_traces", "load_requests", "load_delivery")
SOURCES = ("load_traces", "load_requests")          # the loaders whose source doctor can read
DEFAULT_INTERVAL_S = 900
MAX_INTERVAL_S = 7 * 86400
DEFAULT_DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
TIMEOUT_S = 10
ERROR_SHOWN = 200                                   # characters of a run's error printed
#: The loaders' own source queries; a test holds them equal.
TEMPO_SPAN_NAMES = ("std.artefact.activation", "std.skill.invocation", "std.session.cost")
LOKI_QUERY = '{service_name="claude-code"} | event_name="api_request"'
TEMPO_MAX_WINDOW_S = 168 * 3600                     # query_frontend.search.max_duration
LOKI_WINDOW_S = 168 * 3600                          # how far back Loki is asked; bounds the query
_RUN_REMEDY = "run `stdtel-load` now, and `stdtel-install loader` to keep it running"


@dataclass(frozen=True)
class Run:
    started_at: dt.datetime
    ok: bool
    error: str | None


@dataclass(frozen=True)
class LoaderState:
    latest: Run | None                  # latest attempted run, any status
    watermark: dt.datetime | None       # max(source_max_ts) over successful runs
    any_ok: bool                        # has any run ever succeeded


_NEVER = LoaderState(None, None, False)             # a loader with no row at all


def load_interval_s() -> int:
    raw = os.environ.get("STDTEL_LOAD_INTERVAL", str(DEFAULT_INTERVAL_S))
    try:
        v = int(raw)
    except ValueError:
        v = 0
    if not 0 < v <= MAX_INTERVAL_S:
        raise ValueError(f"STDTEL_LOAD_INTERVAL={raw!r} is not a number of seconds between 1 and {MAX_INTERVAL_S}")
    return v


def _dsn() -> str:
    return os.environ.get("STDTEL_DSN") or DEFAULT_DSN


def read_state(dsn: str | None = None, loaders=LOADERS) -> dict[str, LoaderState]:
    """Latest attempt and watermark per loader, in two queries."""
    import psycopg
    with psycopg.connect(dsn or _dsn(), connect_timeout=TIMEOUT_S,
                         options=f"-c statement_timeout={TIMEOUT_S * 1000}") as conn:
        latest = {r[0]: Run(r[1], bool(r[2]), r[3]) for r in conn.execute(
            "SELECT DISTINCT ON (loader) loader, started_at, ok, error FROM loader_run "
            "WHERE loader = ANY(%s) ORDER BY loader, started_at DESC, run_id DESC", (list(loaders),))}
        ok = {r[0]: r[1] for r in conn.execute(
            "SELECT loader, max(source_max_ts) FROM loader_run WHERE ok AND loader = ANY(%s) "
            "GROUP BY loader", (list(loaders),))}
    return {name: LoaderState(latest.get(name), ok.get(name), name in ok) for name in loaders}


# --- the sources ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def _ns(t: dt.datetime) -> int:
    """Exact nanoseconds since the epoch; float timestamps lose the microseconds."""
    return (t - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)) // dt.timedelta(microseconds=1) * 1000


def _window(since: dt.datetime | None, now: dt.datetime, max_s: int) -> tuple[int, int, bool] | None:
    """(start_ns, end_ns, clamped) to ask a source over, or None when `since` is
    not in the past and nothing can be newer. `clamped` means the window starts
    later than `since`, so finding nothing in it proves nothing."""
    end = _ns(now)
    if since is not None and _ns(since) >= end:
        return None
    floor = end - max_s * 10**9
    start = floor if since is None else max(_ns(since) + 1, floor)
    return start, end, since is None or _ns(since) + 1 < floor


def tempo_has_newer(since: dt.datetime | None, now: dt.datetime, get=None) -> bool | None:
    """Any stdtel span in Tempo starting after `since`? True, False, or None when
    the answer is not knowable (nothing found, but the search had to be clamped).
    One search with limit 1. Tempo takes whole seconds, so the bounds round
    inward: the start up and the end down. The window then never exceeds Tempo's
    maximum and never reaches before the cutoff, at the cost of under a second
    added to the allowance."""
    end_s = _ns(now) // 10**9
    floor_s = end_s - TEMPO_MAX_WINDOW_S
    if since is None:
        start_s, clamped = floor_s, True
    else:
        cut_s = -(-(_ns(since) + 1) // 10**9)          # ceil: strictly after `since`
        if cut_s >= end_s:
            return False                                # cutoff in this second or later
        start_s, clamped = max(cut_s, floor_s), cut_s < floor_s
    get = get or _get_json
    base = os.environ.get("STDTEL_TEMPO", "http://localhost:3200").rstrip("/")
    q = " || ".join(f'name = "{n}"' for n in TEMPO_SPAN_NAMES)
    params = urllib.parse.urlencode({"q": f"{{ {q} }}", "start": start_s, "end": end_s, "limit": 1})
    body = get(f"{base}/api/search?{params}")
    if not isinstance(body, dict) or not isinstance(body.get("traces"), list):
        raise RuntimeError("Tempo search returned no trace list")
    return True if body["traces"] else (None if clamped else False)


def loki_has_newer(since: dt.datetime | None, now: dt.datetime, get=None) -> bool | None:
    """Any Claude Code api_request in Loki after `since`? As tempo_has_newer."""
    w = _window(since, now, LOKI_WINDOW_S)
    if w is None:
        return False
    start_ns, end_ns, clamped = w
    get = get or _get_json
    base = os.environ.get("STDTEL_LOKI", "http://localhost:11010").rstrip("/")
    params = urllib.parse.urlencode({"query": LOKI_QUERY, "start": start_ns, "end": end_ns, "limit": 1})
    body = get(f"{base}/loki/api/v1/query_range?{params}")
    if not isinstance(body, dict) or body.get("status") != "success":
        raise RuntimeError(f"Loki query failed: {(body or {}).get('error') or (body or {}).get('status')}")
    result = (body.get("data") or {}).get("result")
    if not isinstance(result, list):
        raise RuntimeError("Loki returned success with no result list")
    if any(isinstance(st, dict) and st.get("values") for st in result):
        return True
    return None if clamped else False


_NEWER = {"load_traces": tempo_has_newer, "load_requests": loki_has_newer}
_SOURCE = {"load_traces": "Tempo", "load_requests": "Loki"}


def source_has_newer(loader: str, since: dt.datetime | None) -> bool | None:
    return _NEWER[loader](since, dt.datetime.now(dt.timezone.utc))


# --- the judges ----------------------------------------------------------------------------

def _printable(text: str) -> str:
    """A run's error as one line of plain text: no control characters reach a terminal."""
    return "".join(c if c.isprintable() else " " for c in text)


def _age(now: dt.datetime, then: dt.datetime) -> str:
    h = (now - then).total_seconds() / 3600
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


def judge_liveness(state: dict[str, LoaderState], now: dt.datetime, interval_s: int) -> Check:
    """Fail when a loader's latest attempt failed, or none was made within twice the interval."""
    limit = dt.timedelta(seconds=2 * interval_s)
    bad = []
    for name in LOADERS:
        run = state.get(name, _NEVER).latest
        if run is None:
            bad.append(f"{name} has never run")
        elif not run.ok:
            bad.append(f"{name}'s latest run ({_age(now, run.started_at)} ago) failed: "
                       f"{_printable(run.error or 'no error recorded')[:ERROR_SHOWN]}")
        elif now - run.started_at > limit:
            bad.append(f"{name} last ran {_age(now, run.started_at)} ago, past twice the "
                       f"{interval_s // 60}-minute interval")
    if bad:
        failed = any(state.get(n, _NEVER).latest and not state.get(n, _NEVER).latest.ok for n in LOADERS)
        return Check(LIVENESS, FAIL, "; ".join(bad),
                     ("fix the error shown, then " if failed else "") + _RUN_REMEDY)
    return Check(LIVENESS, PASS, f"every loader ran within {2 * interval_s // 60} min, and the latest run of each succeeded")


def judge_freshness(state: dict[str, LoaderState], newer: dict, now: dt.datetime,
                    interval_s: int) -> Check:
    """`newer[loader]` answers "does the source hold anything past the watermark
    plus the allowance?": exactly True or False, or the exception that stopped it
    being asked. Anything else, including None (not knowable) or no answer at
    all, is unknown: a missing answer must never read as "nothing newer".
    Precedence: any stale fails; else any unknown; else pass."""
    stale, unknown, fine = [], [], []
    for name in SOURCES:
        s, answer = state.get(name, _NEVER), newer.get(name)
        if not s.any_ok:
            unknown.append(f"{name} has no successful run, so no watermark")
        elif isinstance(answer, Exception):
            unknown.append(f"{name}: {_SOURCE[name]} unreadable ({type(answer).__name__})")
        elif answer is not True and answer is not False:
            unknown.append(f"{name}: {_SOURCE[name]} could not say whether it holds anything newer "
                           f"(its search window does not reach back to the watermark)")
        elif answer:
            at = f"loaded up to {_age(now, s.watermark)} ago" if s.watermark else "has never loaded a row"
            stale.append(f"{name} {at}, and {_SOURCE[name]} holds newer events past the "
                         f"{2 * interval_s // 60}-minute allowance")
        else:
            fine.append(name)
    if stale:
        return Check(FRESHNESS, FAIL, "; ".join(stale + unknown),
                     _RUN_REMEDY + ". Warehouse figures quoted before then are out of date")
    if unknown:
        return Check(FRESHNESS, UNKNOWN, "; ".join(unknown),
                     _RUN_REMEDY + "; if a source is unreadable, start the stack (`make up`)")
    return Check(FRESHNESS, PASS, f"{', '.join(fine)} hold everything their sources hold "
                 f"(within {2 * interval_s // 60} min)")


# --- the checks ----------------------------------------------------------------------------

_PG_REMEDY = ("start the stack (`make up`), or set STDTEL_DSN; the checks need the warehouse extras: "
              "`uv tool install 'stdtel[warehouse]'`")


def _read(read_state):
    """The loader state, or the Check that says why it could not be read."""
    try:
        return read_state(), None
    except ImportError:
        return None, ("the warehouse extras are not installed (no psycopg)", _PG_REMEDY)
    except Exception as e:                        # noqa: BLE001
        return None, (f"cannot read Postgres: {type(e).__name__}", _PG_REMEDY)


def loader_liveness(read_state=read_state, now: dt.datetime | None = None) -> Check:
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            every = load_interval_s()
        except ValueError as e:
            return Check(LIVENESS, UNKNOWN, str(e), "set STDTEL_LOAD_INTERVAL to the loader's interval in seconds")
        state, why = _read(read_state)
        if why:
            return Check(LIVENESS, UNKNOWN, *why)
        return judge_liveness(state, now=now, interval_s=every)
    except Exception as e:                        # noqa: BLE001 - a diagnostic must not raise
        return Check(LIVENESS, UNKNOWN, f"the check itself failed: {type(e).__name__}: {e}",
                     "this is a bug in stdtel doctor")


def warehouse_freshness(read_state=read_state, newer=source_has_newer, now: dt.datetime | None = None,
                        interval_s: int | None = None) -> Check:
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            every = load_interval_s() if interval_s is None else interval_s
            if isinstance(every, bool) or not isinstance(every, int) or not 0 < every <= MAX_INTERVAL_S:
                raise ValueError(f"interval {every!r} is not a positive number of seconds")
        except ValueError as e:
            return Check(FRESHNESS, UNKNOWN, str(e), "set STDTEL_LOAD_INTERVAL to the loader's interval in seconds")
        state, why = _read(read_state)
        if why:
            return Check(FRESHNESS, UNKNOWN, *why)
        allowance = dt.timedelta(seconds=2 * every)
        answers: dict[str, bool | Exception] = {}
        for name in SOURCES:
            if not state.get(name, _NEVER).any_ok:
                continue
            wm = state[name].watermark
            try:
                answers[name] = newer(name, wm + allowance if wm else None)
            except Exception as e:                # noqa: BLE001 - this source is unknown, not the check
                answers[name] = e
        return judge_freshness(state, answers, now=now, interval_s=every)
    except Exception as e:                        # noqa: BLE001 - a diagnostic must not raise
        return Check(FRESHNESS, UNKNOWN, f"the check itself failed: {type(e).__name__}: {e}",
                     "this is a bug in stdtel doctor")
