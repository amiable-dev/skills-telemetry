"""`stdtel-load`: run every warehouse loader once, under a lock (ADR-015 decision 5).

What a scheduler runs (`stdtel-install loader`, #171), and what a person can run
by hand. One run at a time per machine:

* the lock is a directory (`~/.stdtel/loader.lock`), created atomically, holding
  the owner's PID and process start time;
* an overlapping run exits at once and writes nothing, not even a loader_run row,
  so `loader liveness` notices runs that never happen;
* a lock is reclaimed only when its PID is dead, or alive with a different start
  time (a reused PID), never on age alone, so a long but live run is never doubled.

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
import shutil
import subprocess
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

def process_start(pid: int) -> str | None:
    """The process's start time as `ps` prints it, or None when it is not running.
    Together with the PID it names one process: a reused PID has a different start."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:                             # noqa: BLE001 - no ps: treat as unknown
        return None
    return r.stdout.strip() or None                # a dead PID prints nothing


def _stale(owner_file: Path) -> bool:
    try:
        owner = json.loads(owner_file.read_text())
        pid, start = int(owner["pid"]), owner["start"]
    except (OSError, ValueError, KeyError, TypeError):
        return True                               # half-written or foreign: no live owner can be named
    return process_start(pid) != start            # dead (None) or a reused PID


@contextlib.contextmanager
def lock():
    """Yields True while this process holds the lock, or False when another live
    run does; on False nothing should be done."""
    d = home() / "loader.lock"
    d.parent.mkdir(parents=True, exist_ok=True)
    me = {"pid": os.getpid(), "start": process_start(os.getpid())}
    held = False
    for _ in range(2):
        try:
            d.mkdir()
        except FileExistsError:
            if not _stale(d / "owner"):
                break
            shutil.rmtree(d, ignore_errors=True)  # reclaim, then try once more
            continue
        (d / "owner").write_text(json.dumps(me))
        held = True
        break
    try:
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError, ValueError):
                if json.loads((d / "owner").read_text()) == me:
                    shutil.rmtree(d, ignore_errors=True)


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
    except SystemExit as e:                       # a usage error
        return int(e.code or 0) if isinstance(e.code, int) else 1
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
        artefact = str(home() / "policy-results.jsonl")
        # enrichment, not a precondition: `make load-delivery` carries on without it too
        _run("fetch_policy_results", ["--repo", repo, "--out", artefact])
        rcs.append(_run("load_delivery", ["--repo", repo, "--dsn", dsn, "--policy-results", artefact]))
    else:
        t = _now()
        _record(dsn, {"run_id": uuid.uuid4().hex, "loader": "load_delivery", "started_at": t,
                      "finished_at": t, "rows_loaded": 0, "ok": False,
                      "error": "STDTEL_LOAD_REPO not set: delivery data is not loaded. Set it to "
                               "owner/name in the environment stdtel-load runs in"})
    return 1 if any(rcs) else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stdtel-load", description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("STDTEL_DSN") or DEFAULT_DSN)
    ap.add_argument("--tempo", default=os.environ.get("STDTEL_TEMPO") or "http://localhost:3200")
    ap.add_argument("--loki", default=os.environ.get("STDTEL_LOKI") or "http://localhost:11010")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    log = home() / "loader.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    trim_log(log)
    with lock() as held, log.open("a") as out:
        stamp = _now().isoformat(timespec="seconds")
        if not held:
            print(f"{stamp} another stdtel-load holds the lock; skipped", file=out)
            return 0
        print(f"{stamp} stdtel-load starting", file=out, flush=True)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = run_all(dsn=a.dsn, tempo=a.tempo, loki=a.loki)
        print(f"{_now().isoformat(timespec='seconds')} stdtel-load finished, exit {rc}", file=out)
    print(f"stdtel-load: exit {rc}; see {log}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
