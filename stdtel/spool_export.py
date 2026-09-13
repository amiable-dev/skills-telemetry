"""Drain the spool and export it (ADR-008).

The hook writes NDJSON and returns; this turns those records back into spans and
sends them. Run it on a schedule, from `make up`, or with `--watch`.

    stdtel-export --once
    stdtel-export --watch --interval 30

Export failure leaves the spool untouched, so the next run retries. That is the
whole point: capture keeps working while the collector does not.
"""
from __future__ import annotations

import argparse
import sys
import time

DEFAULT_INTERVAL_S = 30


def export_batch(batch: list[dict], exporter=None) -> int:
    """Rebuild spans from spooled records and emit them.

    Grouped by resource so each group carries the session's own attributes;
    records from different sessions must not be merged under one resource.
    """
    from stdtel.exporter import SESSION_SPAN_NAME, SPAN_NAME, build_provider, emit_invocations, emit_session_cost

    groups: dict[tuple, list[dict]] = {}
    for row in batch:
        key = tuple(sorted((row.get("resource") or {}).items()))
        groups.setdefault(key, []).append(row)

    total = 0
    for key, rows in groups.items():
        provider = build_provider(dict(key), exporter=exporter)
        invocations = [r for r in rows if r.get("name") == SPAN_NAME]
        if invocations:
            by_session: dict[str, list[dict]] = {}
            for r in invocations:
                by_session.setdefault(r.get("session_id", ""), []).append(r)
            for session_id, rs in by_session.items():
                total += emit_invocations(provider, rs, session_id)
        for r in rows:
            if r.get("name") == SESSION_SPAN_NAME:
                total += emit_session_cost(provider, r.get("attributes") or {},
                                           r.get("session_id", ""),
                                           r.get("started_at", 0.0), r.get("ended_at", 0.0))
    return total


def run_once() -> int:
    from stdtel.spool import drain, trim
    dropped = trim()
    if dropped:
        print(f"stdtel-export: spool over its bound, dropped {dropped} oldest record(s)",
              file=sys.stderr)
    try:
        return drain(export_batch)
    except Exception as e:                        # noqa: BLE001 - leave the spool intact
        print(f"stdtel-export: export failed, spool kept for retry: {e}", file=sys.stderr)
        return -1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stdtel-export", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="drain and exit (default)")
    mode.add_argument("--watch", action="store_true", help="drain repeatedly")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if not a.watch:
        n = run_once()
        if n >= 0:
            print(f"exported {n} span(s)")
        return 0 if n >= 0 else 1
    while True:
        n = run_once()
        if n > 0:
            print(f"exported {n} span(s)")
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    raise SystemExit(main())
