"""Load std.skill.invocation spans from Tempo (search API) into Postgres skill_invocation.
Usage: python warehouse/load_traces.py --tempo http://localhost:3200 --dsn postgresql://... --since 24h
Requires: psycopg[binary], requests.
"""
from __future__ import annotations
import argparse, datetime as dt, json, sys

def parse_span(attrs: dict, resource: dict, span: dict) -> dict:
    g = lambda k, d=None: attrs.get(k, resource.get(k, d))
    return {
        "span_id": span["spanID"], "trace_id": span["traceID"], "session_id": g("session.id", ""),
        "started_at": dt.datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, dt.timezone.utc),
        "ended_at": dt.datetime.fromtimestamp((int(span["startTimeUnixNano"]) + int(span.get("durationNanos", 0))) / 1e9, dt.timezone.utc),
        "harness": g("std.harness", "unknown"), "harness_mode": g("std.harness.mode"),
        "skill_name": g("std.skill.name"), "skill_version": g("std.skill.version", "unversioned"),
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

COLS = ["span_id","trace_id","session_id","started_at","ended_at","harness","harness_mode","skill_name","skill_version",
        "standard_id","policy_ids","trigger","model","load_tokens","tail_tokens","tail_tokens_first_only","input_tokens",
        "output_tokens","cache_read_tokens","cache_creation_tokens","llm_requests","is_error","ticket_id","repo","team","user_hash"]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--tempo", required=True); ap.add_argument("--dsn", required=True)
    ap.add_argument("--since", default="24h"); a = ap.parse_args(argv)
    import requests, psycopg
    hours = int(a.since.rstrip("h")); end = int(dt.datetime.now().timestamp()); start = end - hours * 3600
    r = requests.get(f"{a.tempo}/api/search", params={"q": '{ name = "std.skill.invocation" }', "start": start, "end": end, "limit": 1000}); r.raise_for_status()
    rows = []
    for t in r.json().get("traces", []):
        tr = requests.get(f"{a.tempo}/api/traces/{t['traceID']}").json()
        for batch in tr.get("batches", []):
            resource = {kv["key"]: list(kv["value"].values())[0] for kv in batch.get("resource", {}).get("attributes", [])}
            for ss in batch.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    if sp.get("name") != "std.skill.invocation": continue
                    attrs = {kv["key"]: list(kv["value"].values())[0] for kv in sp.get("attributes", [])}
                    rows.append(parse_span(attrs, resource, sp))
    with psycopg.connect(a.dsn) as conn, conn.cursor() as cur:
        sql = f"INSERT INTO skill_invocation ({','.join(COLS)}) VALUES ({','.join('%('+c+')s' for c in COLS)}) ON CONFLICT (span_id) DO NOTHING"
        cur.executemany(sql, rows)
    print(f"loaded {len(rows)} invocation(s)"); return 0

if __name__ == "__main__":
    sys.exit(main())
