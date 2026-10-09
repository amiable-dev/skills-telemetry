"""`stdtel-load`: run every warehouse loader once, under a lock (ADR-015 decision 5).

What a scheduler runs (`stdtel-install loader`, #171), and what a person can run
by hand. One run at a time per user (per `STDTEL_HOME`), by an OS lock on
`~/.stdtel/loader.lock` (see `lock`), which is the scope of the per-user job
that runs it. Runs from two users against one warehouse are safe together:
every write is idempotent or newest-wins, so an overlap costs a repeated query,
never a wrong row (ADR-015 decision 5 says the same of compose plus scheduler). An overlapping run exits at once and writes nothing, not even a
log line or a loader_run row, so `loader liveness` notices runs that never happen.

Each loader's window reaches back to its last successful watermark plus an
overlap, with a 24 h floor and a 720 h cap. A fixed 24 h window cannot repair an
outage longer than a day, and the freshness check would not see the gap: the
next successful run moves the watermark past it. Output goes to
`~/.stdtel/loader.log`, kept to its last megabyte.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import sys
import uuid
from pathlib import Path

LOG_MAX_BYTES = 1024 * 1024
FLOOR_H = 24
CAP_H = 720                  # Loki keeps 720 h here; Tempo's search is chunked (#153)
OVERLAP_H = 2                # past Tempo's 30-minute flush delay, with room; inserts are idempotent
DEFAULT_DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"


def home() -> Path:
    return Path(os.environ.get("STDTEL_HOME") or Path.home() / ".stdtel")


# --- the lock --------------------------------------------------------------------------------

@contextlib.contextmanager
def lock():
    """Yields True while this process holds the machine's loader lock, False when
    another process does; on False nothing should be done.

    An OS lock (flock) on `~/.stdtel/loader.lock`: the kernel grants it atomically
    and releases it when its holder exits, however it exits. So there is no
    staleness to judge, a dead holder's lock is free, a live holder's never is,
    and a reused PID cannot matter. The file's content (PID, time) is for a person
    reading it; nothing decides on it. The same mechanism as session state
    (`stdtel.state.locked`).
    """
    import fcntl
    path = home() / "loader.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pid": os.getpid(), "since": _now().isoformat(timespec="seconds")}))
        f.flush()
        yield True                                # closing the file releases the lock


# --- the log ---------------------------------------------------------------------------------

def trim_log(log: Path) -> None:
    """Keep the last LOG_MAX_BYTES, starting at a line boundary."""
    try:
        if log.stat().st_size <= LOG_MAX_BYTES:
            return
    except FileNotFoundError:
        return
    with log.open("rb") as f:
        f.seek(-LOG_MAX_BYTES, os.SEEK_END)
        tail = f.read()
    cut = tail.find(b"\n")
    log.write_bytes(tail[cut + 1:] if cut >= 0 else tail)


# --- the window ------------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def window_hours(watermark: dt.datetime | None, now: dt.datetime) -> int:
    """Back to the watermark plus the overlap, within [FLOOR_H, CAP_H]."""
    if watermark is None:
        return FLOOR_H
    behind = math.ceil((now - watermark).total_seconds() / 3600) + OVERLAP_H
    return max(FLOOR_H, min(CAP_H, behind))


def watermarks(dsn: str) -> dict[str, dt.datetime]:
    import psycopg
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        return dict(conn.execute("SELECT loader, max(source_max_ts) FROM loader_run WHERE ok "
                                 "GROUP BY loader").fetchall())


# --- the run ---------------------------------------------------------------------------------

def _run(name: str, argv: list[str]) -> int:
    """One loader's main(), in this process. Anything it raises is its failure, not the run's."""
    import importlib
    try:
        return int(importlib.import_module(f"warehouse.{name}").main(argv) or 0)
    except SystemExit as e:                       # as Python reads it: None 0, an int itself, text 1
        return 0 if e.code is None else e.code if isinstance(e.code, int) else 1
    except Exception as e:                        # noqa: BLE001
        print(f"{name} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _record(dsn: str, row: dict) -> bool:
    from warehouse.load_traces import LOADER_RUN_COLS, record_run
    return record_run(dsn, {c: row.get(c) for c in LOADER_RUN_COLS})


def run_all(dsn: str, tempo: str, loki: str) -> int:
    """Traces, requests, delivery, in that order. One failing never stops the
    rest; the exit is 1 if any loader failed."""
    try:
        marks = watermarks(dsn)
    except Exception as e:                        # noqa: BLE001 - load anyway, from the floor
        print(f"watermarks unreadable ({type(e).__name__}); loading the last {FLOOR_H} h", file=sys.stderr)
        marks = {}
    now = _now()
    since = lambda name: f"{window_hours(marks.get(name), now)}h"   # noqa: E731
    rcs = [_run("load_traces", ["--tempo", tempo, "--dsn", dsn, "--since", since("load_traces")]),
           _run("load_requests", ["--loki", loki, "--dsn", dsn, "--since", since("load_requests")])]
    repo = os.environ.get("STDTEL_LOAD_REPO")
    if repo:
        artefact = home() / "policy-results.jsonl"
        # enrichment, not a precondition: `make load-delivery` carries on without it too.
        # A previous run's file is removed first, so a failed fetch never passes it off as today's.
        artefact.unlink(missing_ok=True)
        fetched = _run("fetch_policy_results", ["--repo", repo, "--out", str(artefact)]) == 0
        extra = ["--policy-results", str(artefact)] if fetched and artefact.exists() else []
        rcs.append(_run("load_delivery", ["--repo", repo, "--dsn", dsn, *extra]))
    else:
        # a failed run, recorded so `loader liveness` names the setting, and counted
        t = _now()
        try:
            written = _record(dsn, {"run_id": uuid.uuid4().hex, "loader": "load_delivery", "started_at": t,
                                    "finished_at": t, "rows_loaded": 0, "ok": False,
                                    "error": "STDTEL_LOAD_REPO not set: delivery data is not loaded. Set it "
                                             "to owner/name in the environment stdtel-load runs in"})
        except Exception as e:                    # noqa: BLE001
            written = False
            print(f"load_delivery: could not record its run: {type(e).__name__}", file=sys.stderr)
        if not written:
            print("load_delivery: STDTEL_LOAD_REPO is not set and the run could not be recorded",
                  file=sys.stderr)
        rcs.append(1)
    return 1 if any(rcs) else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stdtel-load", description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("STDTEL_DSN") or DEFAULT_DSN)
    ap.add_argument("--tempo", default=os.environ.get("STDTEL_TEMPO") or "http://localhost:3200")
    ap.add_argument("--loki", default=os.environ.get("STDTEL_LOKI") or "http://localhost:11010")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    log = home() / "loader.log"
    with lock() as held:
        if not held:                              # nothing written: no log line, no loader_run row
            print("stdtel-load: another run holds the lock; skipped", file=sys.stderr)
            return 0
        missing = _extras_missing()
        if missing:
            print(f"stdtel-load: {', '.join(missing)} not installed; the loaders need the warehouse "
                  "extras: uv tool install 'stdtel[warehouse]'", file=sys.stderr)
            return 2
        trim_log(log)                             # only the lock holder touches the log
        with log.open("a") as out:
            print(f"{_now().isoformat(timespec='seconds')} stdtel-load starting", file=out, flush=True)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    rc = run_all(dsn=a.dsn, tempo=a.tempo, loki=a.loki)
                except Exception as e:            # noqa: BLE001 - the log still says how it ended
                    print(f"stdtel-load failed: {type(e).__name__}: {e}")
                    rc = 1
            print(f"{_now().isoformat(timespec='seconds')} stdtel-load finished, exit {rc}", file=out)
        trim_log(log)                             # and a verbose run does not leave it over the bound
    print(f"stdtel-load: exit {rc}; see {log}")
    return rc


def _extras_missing() -> list[str]:
    """The warehouse extras this command needs and cannot import."""
    out = []
    for mod in ("psycopg", "requests"):
        try:
            __import__(mod)
        except Exception:                         # noqa: BLE001 - installed but unusable is missing too
            out.append(mod)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
