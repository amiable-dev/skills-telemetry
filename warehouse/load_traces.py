"""Load std.skill.invocation spans from Tempo (search API) into Postgres skill_invocation.
Usage: python warehouse/load_traces.py --tempo http://localhost:3200 --dsn postgresql://... --since 24h
Requires: psycopg[binary], requests.
"""
from __future__ import annotations
import argparse, base64, binascii, datetime as dt, json, sys

def span_id(span: dict, *keys: str) -> str:
    """Hex id from a Tempo span.

    /api/search returns hex under spanID/traceID; /api/traces/{id} returns OTLP
    JSON with base64 under spanId/traceId. The loader reads the second, so decode.
    """
    for k in keys:
        v = span.get(k)
        if v:
            try:
                return base64.b64decode(v).hex()
            except (ValueError, binascii.Error):
                return str(v)          # already hex (search API shape)
    return ""


def end_nanos(span: dict) -> int:
    end = span.get("endTimeUnixNano")
    if end:
        return int(end)
    return int(span["startTimeUnixNano"]) + int(span.get("durationNanos", 0))


def parse_span(attrs: dict, resource: dict, span: dict) -> dict:
    g = lambda k, d=None: attrs.get(k, resource.get(k, d))
    return {
        "span_id": span_id(span, "spanId", "spanID"),
        "trace_id": span_id(span, "traceId", "traceID"),
        "session_id": g("session.id", ""),
        "started_at": dt.datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, dt.timezone.utc),
        "ended_at": dt.datetime.fromtimestamp(end_nanos(span) / 1e9, dt.timezone.utc),
        "harness": g("std.harness", "unknown"), "harness_mode": g("std.harness.mode"),
        "skill_name": g("std.skill.name"),
        "invoked_as": g("std.skill.invoked_as", g("std.skill.name")),
        "plugin": g("std.skill.plugin"),
        "skill_version": g("std.skill.version", "unversioned"),
        "standard_id": g("std.standard_id"), "policy_ids": [p for p in (g("std.policy.ids", "") or "").split(",") if p],
        "trigger": g("std.skill.trigger"), "model": g("gen_ai.request.model"),
        "load_tokens": int(g("std.skill.load_tokens", 0)), "tail_tokens": int(g("std.skill.tail_tokens", 0)),
        "tail_tokens_first_only": int(g("std.skill.tail_tokens_first_only", 0)),
        "input_tokens": int(g("gen_ai.usage.input_tokens", 0)), "output_tokens": int(g("gen_ai.usage.output_tokens", 0)),
        "cache_read_tokens": int(g("gen_ai.usage.cache_read_input_tokens", 0)),
        "cache_creation_tokens": int(g("gen_ai.usage.cache_creation_input_tokens", 0)),
        "llm_requests": int(g("std.skill.llm_requests", 0)),
        "is_error": span.get("status", {}).get("code") == "STATUS_CODE_ERROR",
        "ticket_id": g("std.ticket.id", "unattributed"), "repo": g("std.repo"), "team": g("std.team"),
        "user_hash": g("std.user.hash"),
    }

SESSION_COLS = ["session_id", "harness", "model", "ticket_id", "team", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                "cost_usd", "active_seconds", "started_at"]


def parse_session(attrs: dict, resource: dict, span: dict) -> dict:
    """std.session.cost -> session_cost. This is total spend for the session,
    which overlaps skill_invocation.tail_tokens by design; never sum the two."""
    g = lambda k, d=None: attrs.get(k, resource.get(k, d))
    start = int(span["startTimeUnixNano"])
    return {
        "session_id": g("session.id", ""),
        "harness": g("std.harness", "unknown"),
        "model": g("gen_ai.request.model"),
        "ticket_id": g("std.ticket.id", "unattributed"),
        "team": g("std.team"),
        "input_tokens": int(g("gen_ai.usage.input_tokens", 0)),
        "output_tokens": int(g("gen_ai.usage.output_tokens", 0)),
        "cache_read_tokens": int(g("gen_ai.usage.cache_read_input_tokens", 0)),
        "cache_creation_tokens": int(g("gen_ai.usage.cache_creation_input_tokens", 0)),
        "cost_usd": None,                       # priced downstream; harnesses differ in currency
        # a span whose start is near the epoch would otherwise yield "seconds
        # since 1970"; anything beyond a day of wall clock is not a session
        "active_seconds": min(86_400, max(0, (end_nanos(span) - start) // 1_000_000_000)),
        "started_at": dt.datetime.fromtimestamp(start / 1e9, dt.timezone.utc),
    }


COLS = ["span_id","trace_id","session_id","started_at","ended_at","harness","harness_mode","skill_name",
        "invoked_as","plugin","skill_version",
        "standard_id","policy_ids","trigger","model","load_tokens","tail_tokens","tail_tokens_first_only","input_tokens",
        "output_tokens","cache_read_tokens","cache_creation_tokens","llm_requests","is_error","ticket_id","repo","team","user_hash"]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--tempo", required=True); ap.add_argument("--dsn", required=True)
    ap.add_argument("--since", default="24h"); a = ap.parse_args(argv)
    import requests, psycopg
    hours = int(a.since.rstrip("h")); end = int(dt.datetime.now().timestamp()); start = end - hours * 3600
    def search(query):
        # Tempo returns nothing at all without start/end — it looks like a dead pipeline
        r = requests.get(f"{a.tempo}/api/search",
                         params={"q": query, "start": start, "end": end, "limit": 1000})
        r.raise_for_status()
        return r.json().get("traces", [])

    traces = {t["traceID"]: t for t in search('{ name = "std.skill.invocation" }')}
    traces.update({t["traceID"]: t for t in search('{ name = "std.session.cost" }')})
    rows, session_rows = [], []
    for t in traces.values():
        tr = requests.get(f"{a.tempo}/api/traces/{t['traceID']}").json()
        for batch in tr.get("batches", []):
            resource = {kv["key"]: list(kv["value"].values())[0] for kv in batch.get("resource", {}).get("attributes", [])}
            for ss in batch.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    attrs = {kv["key"]: list(kv["value"].values())[0] for kv in sp.get("attributes", [])}
                    if sp.get("name") == "std.skill.invocation":
                        rows.append(parse_span(attrs, resource, sp))
                    elif sp.get("name") == "std.session.cost":
                        session_rows.append(parse_session(attrs, resource, sp))
    with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
        for table, cols, data, key in (("skill_invocation", COLS, rows, "span_id"),
                                       ("session_cost", SESSION_COLS, session_rows, "session_id")):
            if not data:
                continue
            sql = (f"INSERT INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('%(' + c + ')s' for c in cols)}) "
                   f"ON CONFLICT ({key}) DO NOTHING")
            cur.executemany(sql, [{c: r.get(c) for c in cols} for r in data])
    print(f"loaded {len(rows)} invocation(s), {len(session_rows)} session cost row(s)"); return 0

if __name__ == "__main__":
    sys.exit(main())
