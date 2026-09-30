"""Load harness-native model requests into `llm_request` (ADR-014 decision 5, #112).

    python -m warehouse.load_requests --loki http://localhost:11010 --dsn postgresql://... --since 24h

Claude Code's `api_request` event is its own record of one model request: tokens,
cost, model, and the skill, agent, plugin and MCP server that request served.
The collector sends events to Loki (decision 4), so this reads Loki.

Two things about Loki shape this file. Its OTLP ingestion flattens attribute
names — `skill.name` is stored as `skill_name` — so every key below is Loki's
spelling. And `query_range` stops at `limit` without saying there was more, so
`fetch_all` pages forward until a page comes back short; a loader that drops
requests silently is exactly the failure ADR-005 forbids.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation

from warehouse.load_traces import record_run, write

LOADER = "load_requests"
HARNESS = "claude-code"
#: Only api_request, filtered in Loki; parse_values checks again, so a widened
#: query can never turn a hook event into a row.
QUERY = '{service_name="claude-code"} | event_name="api_request"'
#: Without this, Loki merges structured metadata into the stream labels and
#: returns one stream per distinct request.
HEADERS = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}
PAGE = 5000
PROBE_SERVICE = "stdtel-probe"

REQUEST_COLS = ["harness", "request_id", "session_id", "prompt_id", "conversation_id", "trace_id",
                "ended_at", "duration_ms", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_creation_tokens", "cost_usd", "skill_name", "agent_name",
                "plugin_name", "mcp_server", "mcp_tool", "query_source", "branch_hash", "user_hash",
                "attribution_source"]

#: column <- Loki's name for the attribute Claude Code sends.
_TEXT = {"session_id": "session_id", "prompt_id": "prompt_id", "model": "model",
         "skill_name": "skill_name", "agent_name": "agent_name", "plugin_name": "plugin_name",
         "mcp_server": "mcp_server_name", "mcp_tool": "mcp_tool_name",
         "query_source": "query_source", "user_hash": "std_user_hash"}
_INT = {"duration_ms": "duration_ms", "input_tokens": "input_tokens", "output_tokens": "output_tokens",
        "cache_read_tokens": "cache_read_tokens", "cache_creation_tokens": "cache_creation_tokens"}


def _meta(value: list) -> dict:
    extra = value[2] if len(value) > 2 and isinstance(value[2], dict) else {}
    return {**extra.get("parsed", {}), **extra.get("structuredMetadata", {})}


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


def parse_values(values: list, stream: dict | None = None) -> tuple[list[dict], int]:
    """(rows, rejected). A request with no id cannot be keyed, so it is counted
    and reported rather than dropped without a trace."""
    rows, rejected = [], 0
    for v in values:
        m = {**(stream or {}), **_meta(v)}
        if m.get("event_name") != "api_request" or m.get("service_name") == PROBE_SERVICE:
            continue
        if not m.get("request_id"):
            rejected += 1
            continue
        row = {c: None for c in REQUEST_COLS}
        row.update({col: (m.get(key) or None) for col, key in _TEXT.items()})
        row.update({col: _int(m.get(key)) for col, key in _INT.items()})
        row.update(harness=HARNESS, request_id=m["request_id"], cost_usd=_decimal(m.get("cost_usd")),
                   attribution_source="native",
                   ended_at=dt.datetime.fromtimestamp(0, dt.timezone.utc)
                   + dt.timedelta(microseconds=int(v[0]) // 1000))
        rows.append(row)
    return rows, rejected


def fetch_all(page, start_ns: int, end_ns: int, limit: int = PAGE) -> list:
    """Every entry in [start_ns, end_ns), however many pages that takes.

    `page(start_ns, end_ns, limit)` returns entries oldest first. The next page
    starts at the last timestamp seen, not after it, because two entries can
    share a nanosecond across a page boundary; entries already seen are skipped.
    """
    out, seen, start = [], set(), start_ns
    while True:
        got = page(start, end_ns, limit)
        fresh = [e for e in got if (e[0], json.dumps(e[2], sort_keys=True)) not in seen]
        for e in fresh:
            seen.add((e[0], json.dumps(e[2], sort_keys=True)))
        out.extend(fresh)
        if len(got) < limit:
            return out
        last = int(got[-1][0])
        if last == start and not fresh:
            raise RuntimeError(f"more than {limit} entries share the same nanosecond ({last}); "
                               "raise --page, or the rest of them would be skipped")
        start = last


def fetch_page_from_loki(base: str):
    """A `page` function reading Loki's query_range, oldest first."""
    import requests

    def page(start_ns: int, end_ns: int, limit: int) -> list:
        r = requests.get(f"{base.rstrip('/')}/loki/api/v1/query_range", headers=HEADERS, timeout=30,
                         params={"query": QUERY, "start": str(start_ns), "end": str(end_ns),
                                 "limit": limit, "direction": "forward"})
        r.raise_for_status()
        entries = []
        for s in r.json()["data"]["result"]:
            entries.extend([*v[:2], {**(v[2] if len(v) > 2 else {}),
                                     "structuredMetadata": {**s.get("stream", {}),
                                                            **(v[2] if len(v) > 2 else {}).get("structuredMetadata", {})}}]
                           for v in s["values"])
        entries.sort(key=lambda e: int(e[0]))
        return entries[:limit]
    return page


def new_run_id() -> str:
    return uuid.uuid4().hex


def parse_args(argv=None) -> argparse.Namespace:
    """--loki and --dsn fall back to STDTEL_LOKI and STDTEL_DSN, which is all the
    compose loader sets (#121: a required flag it never passed failed every run)."""
    import os
    ap = argparse.ArgumentParser(prog="load_requests", description=__doc__.splitlines()[0])
    ap.add_argument("--loki", default=os.environ.get("STDTEL_LOKI") or None)
    ap.add_argument("--dsn", default=os.environ.get("STDTEL_DSN") or None)
    ap.add_argument("--since", default="24h")
    ap.add_argument("--start-ns", type=int, help=argparse.SUPPRESS)   # tests: a fixed window
    ap.add_argument("--page", type=int, default=PAGE)
    a = ap.parse_args(argv)
    for flag, env in (("loki", "STDTEL_LOKI"), ("dsn", "STDTEL_DSN")):
        if not getattr(a, flag):
            ap.error(f"--{flag} is required (or set {env})")
    return a


def main(argv=None) -> int:
    a = parse_args(argv)
    end_ns = time.time_ns() + 10**9
    start_ns = a.start_ns if a.start_ns is not None else end_ns - int(a.since.rstrip("h")) * 3600 * 10**9
    run = {"run_id": new_run_id(), "loader": LOADER, "started_at": dt.datetime.now(dt.timezone.utc),
           "finished_at": None, "rows_loaded": 0, "source_max_ts": None, "ok": False, "error": None,
           "unknown_attrs": None, "unknown_attr_keys": None}
    try:
        rows, rejected = parse_values(fetch_all(fetch_page_from_loki(a.loki), start_ns, end_ns, a.page))
        run["source_max_ts"] = max((r["ended_at"] for r in rows), default=None)
        run["rows_loaded"] = write(a.dsn, [("llm_request", REQUEST_COLS, rows, "harness, request_id")])
        if rejected:
            raise RuntimeError(f"{rejected} api_request event(s) had no request_id and were not loaded; "
                               f"{len(rows)} were")
        run["ok"] = True
    except Exception as e:                      # noqa: BLE001 - every run writes a row
        run["error"] = f"{type(e).__name__}: {e}"[:2000]
        run["finished_at"] = dt.datetime.now(dt.timezone.utc)
        record_run(a.dsn, run)
        print(f"load failed: {run['error']}", file=sys.stderr)
        return 1
    run["finished_at"] = dt.datetime.now(dt.timezone.utc)
    record_run(a.dsn, run)
    print(f"loaded {len(rows)} model request(s) from Loki")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
