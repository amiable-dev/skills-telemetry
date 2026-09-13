"""Append spans to disk; export them from somewhere else (ADR-008).

A hook that never opens a socket cannot stall on one. That is what "hooks never
block the developer" was always reaching for — bounding the export timeout only
traded a 7.34s stall for silent data loss.

The file is NDJSON, one span per line, under `~/.stdtel/spool/`. `scrub()` runs
before anything is written: the content rules apply to disk, not only to the wire.

Draining is deliberately conservative. Records are removed only after the export
returns, so a failure leaves everything in place, and the drain rewrites only what
it actually read — a hook firing mid-drain is not lost.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_MAX_RECORDS = 50_000


class SpoolFull(RuntimeError):
    pass


def spool_dir() -> Path:
    d = Path(os.environ.get("STDTEL_SPOOL_DIR", Path.home() / ".stdtel" / "spool"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def spool_path() -> Path:
    return spool_dir() / "spans.ndjson"


def append(records: Iterable[dict]) -> int:
    """Write records and return. No network, no retry, no blocking."""
    from stdtel.exporter import scrub

    rows = []
    for r in records:
        row = dict(r)
        row["attributes"] = scrub(dict(row.get("attributes") or {}))
        row["resource"] = scrub(dict(row.get("resource") or {}))
        rows.append(row)
    if not rows:
        return 0
    with spool_path().open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return len(rows)


def read_all() -> list[dict]:
    """Every readable record. A corrupt line is skipped, not fatal — one bad
    write must not strand everything behind it."""
    path = spool_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite(rows: list[dict]) -> None:
    """Replace the spool atomically, so a crash mid-rewrite cannot truncate it."""
    path = spool_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def drain(export: Callable[[list[dict]], None]) -> int:
    """Hand every spooled record to `export`, then remove exactly those.

    Records appended while `export` runs are kept: the rewrite drops only the
    records that were read, matched by position, rather than truncating the file.
    A raising export removes nothing.
    """
    batch = read_all()
    if not batch:
        return 0
    export(batch)                       # may raise; nothing is removed if it does
    remaining = read_all()[len(batch):]
    _rewrite(remaining)
    return len(batch)


def max_records_default() -> int:
    try:
        return max(1, int(os.environ.get("STDTEL_SPOOL_MAX", DEFAULT_MAX_RECORDS)))
    except ValueError:
        return DEFAULT_MAX_RECORDS


def trim(max_records: int | None = None) -> int:
    """Enforce the bound, oldest first. Returns how many were dropped.

    The count is the point: an unbounded spool fills a developer's disk, and a
    silent drop at the bound would reintroduce exactly the invisible loss this
    design removes.
    """
    max_records = max_records_default() if max_records is None else max_records
    rows = read_all()
    if len(rows) <= max_records:
        return 0
    dropped = len(rows) - max_records
    _rewrite(rows[dropped:])
    return dropped
