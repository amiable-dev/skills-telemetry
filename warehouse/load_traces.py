"""Load artefact activations and session cost from Tempo into Postgres.

    python warehouse/load_traces.py --tempo http://localhost:3200 --dsn postgresql://... --since 24h

ADR-009 renamed the wire-level span `std.skill.invocation` to
`std.artefact.activation{kind=skill}` in one release, with no dual emission —
two spans would have doubled every spanmetrics series and both dashboards.
Compatibility lives *here*, one layer down: every activation is written to
`artefact_activation`, and the ones of kind `skill` are additionally written to
`skill_invocation` exactly as before, so `scorecard.sql`, every existing query
and every row recorded before the ADR keep working unchanged.

Both span names are searched. Rows emitted before the rename are still in Tempo
and carry no `std.artefact.kind`; they are skills by definition, and are loaded
as such.

Requires: psycopg[binary], requests.
"""
from __future__ import annotations
import argparse, base64, binascii, datetime as dt, json, sys, uuid

SPAN_NAME = "std.artefact.activation"
LEGACY_SKILL_SPAN_NAME = "std.skill.invocation"   # pre-ADR-009 rows still in Tempo
SESSION_SPAN_NAME = "std.session.cost"
KIND_SKILL = "skill"
LOADER = "load_traces"

#: Tempo does not make a span searchable the moment it arrives. A span whose own
#: timestamp is in the past — which every `subagent` activation has, since it
#: carries the sub-agent's start and end and is not sent until the parent's next
#: Stop — only appears once the ingester flushes its block. Measured on the local
#: stack: a span stamped 90 minutes back was invisible to search immediately and
#: present ~30 minutes later, with nothing discarded and no error anywhere (#67).
#: The window must therefore comfortably exceed the flush delay, because a run
#: that steps over a span never returns for it. Overlap is free: every row is
#: keyed on span_id and re-inserting one is a no-op.
TEMPO_FLUSH_MINUTES = 30        # complete_block_timeout 15m, max_block_duration 30m
MIN_SAFE_WINDOW_HOURS = 2

#: `std.turn.hook.<basename>.ms` cannot be enumerated in advance: the set of
#: hooks is the developer's, not ours. The basename is all that is recorded —
#: the payload carries an absolute path, which is filesystem layout, not data.
_HOOK_PREFIX, _HOOK_SUFFIX = "std.turn.hook.", ".ms"

#: The schema is discriminated by kind, so the loader reads each kind's own
#: attribute rather than guessing across all of them. A sub-agent's request
#: count and a turn's are different measurements that happen to share a column.
_LLM_REQUESTS = {"skill": "std.skill.llm_requests", "subagent": "std.subagent.llm_requests",
                 "turn": "std.turn.llm_requests"}
_TOOL_CALLS = {"subagent": "std.subagent.tool_calls", "turn": "std.turn.tool_calls"}
_DURATION_MS = {"skill": "std.skill.duration_ms", "subagent": "std.subagent.duration_ms",
                "turn": "std.turn.duration_ms"}


#: A span id is 8 bytes, a trace id 16. Nothing else is either, which is the
#: only reliable way to tell OTLP's base64 from the search API's hex: a 16-char
#: hex span id is *also* valid base64, and b64decode would silently turn it into
#: 12 bytes of a different id rather than raising.
_ID_BYTES = (8, 16)


def span_id(span: dict, *keys: str) -> str:
    """Hex id from a Tempo span.

    /api/search returns hex under spanID/traceID; /api/traces/{id} returns OTLP
    JSON with base64 under spanId/traceId. The loader reads the second, so
    decode — but only when the result is the length an id actually is.
    """
    for k in keys:
        v = span.get(k)
        if v:
            try:
                raw = base64.b64decode(v)
            except (ValueError, binascii.Error):
                return str(v)          # not base64 at all: search API shape
            return raw.hex() if len(raw) in _ID_BYTES else str(v)
    return ""


def end_nanos(span: dict) -> int:
    end = span.get("endTimeUnixNano")
    if end:
        return int(end)
    return int(span["startTimeUnixNano"]) + int(span.get("durationNanos", 0))


def _int(value) -> int | None:
    """An observed integer, or None.

    Never 0 for an absent attribute (ADR-005): a sub-agent that read no cached
    tokens and a sub-agent whose cache reads we never saw are different facts,
    and `sum()` over a column that conflates them is not a measurement.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_span(attrs: dict, resource: dict, span: dict) -> dict:
    """An activation of kind `skill` -> the pre-ADR-009 `skill_invocation` row.

    Deliberately unchanged, including its DEFAULT-0 token columns: rows loaded
    before ADR-009 were written this way, and making old and new rows mean
    different things in one column is worse than the zero.
    """
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
        "content_hash": g("std.skill.content_hash"),
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


def hook_breakdown(attrs: dict) -> str | None:
    """`{hook basename: ms}` as JSON, or None when no hook latency was observed.

    An empty object would claim the turn ran no hooks; the Stop hook itself
    always runs, so an empty object is never true — it means the harness did not
    report `hookInfos`, which is missing data.
    """
    out = {k[len(_HOOK_PREFIX):-len(_HOOK_SUFFIX)]: _int(v) for k, v in attrs.items()
           if k.startswith(_HOOK_PREFIX) and k.endswith(_HOOK_SUFFIX)}
    out = {k: v for k, v in out.items() if v is not None}
    return json.dumps(out, sort_keys=True) if out else None


def parse_activation(attrs: dict, resource: dict, span: dict) -> dict:
    """Any `std.artefact.activation` -> one `artefact_activation` row.

    A legacy `std.skill.invocation` span carries no `std.artefact.kind`; it is a
    skill by definition and is read as one, which is what keeps every row
    recorded before ADR-009 loading.
    """
    g = lambda k, d=None: attrs.get(k, resource.get(k, d))
    kind = g("std.artefact.kind") or KIND_SKILL
    usage = {
        "input_tokens": _int(g("gen_ai.usage.input_tokens")),
        "output_tokens": _int(g("gen_ai.usage.output_tokens")),
        "cache_read_tokens": _int(g("gen_ai.usage.cache_read_input_tokens")),
        "cache_creation_tokens": _int(g("gen_ai.usage.cache_creation_input_tokens")),
    }
    return {
        "span_id": span_id(span, "spanId", "spanID"),
        "trace_id": span_id(span, "traceId", "traceID"),
        "session_id": g("session.id", ""),
        "started_at": dt.datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, dt.timezone.utc),
        "ended_at": dt.datetime.fromtimestamp(end_nanos(span) / 1e9, dt.timezone.utc),
        "kind": kind,
        # a turn carries no name: its identity is prompt_id, which is unbounded
        "name": g("std.artefact.name") or (g("std.skill.name") if kind == KIND_SKILL else None),
        "source": g("std.artefact.source"),
        "harness": g("std.harness", "unknown"),
        "harness_mode": g("std.harness.mode"),
        "prompt_id": g("std.prompt.id"),
        "parent_prompt_id": g("std.artefact.parent_prompt_id"),
        "model": g("gen_ai.request.model"),
        **usage,
        "llm_requests": _int(g(_LLM_REQUESTS.get(kind, ""))),
        "tool_calls": _int(g(_TOOL_CALLS.get(kind, ""))),
        "duration_ms": _int(g(_DURATION_MS.get(kind, ""))),
        "is_error": span.get("status", {}).get("code") == "STATUS_CODE_ERROR",
        "subagent_type": g("std.subagent.type"),
        "subagent_id": g("std.subagent.id"),
        "subagent_depth": _int(g("std.subagent.depth")),
        "compaction_reason": g("std.compaction.reason"),
        "compaction_tokens_before": _int(g("std.compaction.tokens_before")),
        "compaction_tokens_after": _int(g("std.compaction.tokens_after")),
        "compaction_turns_since_previous": _int(g("std.compaction.turns_since_previous")),
        "hook_ms": _int(g("std.turn.hook_ms")),
        "hook_ms_by_hook": hook_breakdown(attrs),
        "ticket_id": g("std.ticket.id", "unattributed"),
        "repo": g("std.repo"), "team": g("std.team"), "user_hash": g("std.user.hash"),
    }


SESSION_COLS = ["session_id", "harness", "model", "ticket_id", "team", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                "cost_usd", "api_ms", "tool_ms", "duration_ms", "active_seconds", "started_at"]


def parse_session(attrs: dict, resource: dict, span: dict) -> dict:
    """std.session.cost -> session_cost. This is total spend for the session,
    which overlaps skill_invocation.tail_tokens by design; never sum the two."""
    g = lambda k, d=None: attrs.get(k, resource.get(k, d))
    start = int(span["startTimeUnixNano"])
    cost = g("std.session.cost_usd")
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
        # Real money, from the harness's own cost-state entry (ADR-009). NULL
        # where the harness never reported it — a session that cost nothing and
        # a session whose cost we never saw are not the same row.
        "cost_usd": float(cost) if isinstance(cost, (int, float)) or (
            isinstance(cost, str) and cost.replace(".", "", 1).isdigit()) else None,
        "api_ms": _int(g("std.session.api_ms")),
        "tool_ms": _int(g("std.session.tool_ms")),
        "duration_ms": _int(g("std.session.duration_ms")),
        # a span whose start is near the epoch would otherwise yield "seconds
        # since 1970"; anything beyond a day of wall clock is not a session
        "active_seconds": min(86_400, max(0, (end_nanos(span) - start) // 1_000_000_000)),
        "started_at": dt.datetime.fromtimestamp(start / 1e9, dt.timezone.utc),
    }


COLS = ["span_id","trace_id","session_id","started_at","ended_at","harness","harness_mode","skill_name",
        "invoked_as","plugin","skill_version","content_hash",
        "standard_id","policy_ids","trigger","model","load_tokens","tail_tokens","tail_tokens_first_only","input_tokens",
        "output_tokens","cache_read_tokens","cache_creation_tokens","llm_requests","is_error","ticket_id","repo","team","user_hash"]

ACTIVATION_COLS = ["span_id", "trace_id", "session_id", "started_at", "ended_at", "kind", "name",
                   "source", "harness", "harness_mode", "prompt_id", "parent_prompt_id", "model",
                   "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                   "llm_requests", "tool_calls", "duration_ms", "is_error",
                   "subagent_type", "subagent_id", "subagent_depth",
                   "compaction_reason", "compaction_tokens_before", "compaction_tokens_after",
                   "compaction_turns_since_previous", "hook_ms", "hook_ms_by_hook",
                   "ticket_id", "repo", "team", "user_hash"]

LOADER_RUN_COLS = ["run_id", "started_at", "finished_at", "loader", "rows_loaded",
                   "source_max_ts", "ok", "error"]


def _attrs(items: list) -> dict:
    return {kv["key"]: list(kv["value"].values())[0] for kv in items or []}


def collect(traces: dict, fetch) -> tuple[list[dict], list[dict], list[dict]]:
    """(activations, skill_invocations, session_costs) from a set of trace ids.

    `fetch(trace_id) -> OTLP JSON`, so the parsing is testable without Tempo.
    """
    activations, rows, session_rows = [], [], []
    for trace_id in traces:
        tr = fetch(trace_id)
        for batch in tr.get("batches", []):
            resource = _attrs(batch.get("resource", {}).get("attributes", []))
            for ss in batch.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    attrs = _attrs(sp.get("attributes", []))
                    name = sp.get("name")
                    if name in (SPAN_NAME, LEGACY_SKILL_SPAN_NAME):
                        act = parse_activation(attrs, resource, sp)
                        activations.append(act)
                        if act["kind"] == KIND_SKILL:
                            # ADR-009's compatibility decision lives here and
                            # nowhere else: one span, two tables, so the primary
                            # metric never notices the rename.
                            rows.append(parse_span(attrs, resource, sp))
                    elif name == SESSION_SPAN_NAME:
                        session_rows.append(parse_session(attrs, resource, sp))
    return activations, rows, session_rows


def write(dsn: str, batches: list[tuple[str, list[str], list[dict], str]]) -> int:
    """Insert each (table, cols, rows, conflict key) batch. Returns rows offered."""
    import psycopg
    total = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table, cols, data, key in batches:
            if not data:
                continue
            sql = (f"INSERT INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('%(' + c + ')s' for c in cols)}) "
                   f"ON CONFLICT ({key}) DO NOTHING")
            cur.executemany(sql, [{c: r.get(c) for c in cols} for r in data])
            total += len(data)
    return total


def record_run(dsn: str, row: dict) -> bool:
    """Write the run's own row. Failure to do so is reported, never raised.

    A loader that records only its successes is indistinguishable from one that
    was never run, which is the failure this table exists to make visible — but
    losing the run's real error behind a bookkeeping error would be worse.
    """
    try:
        import psycopg
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO loader_run ({','.join(LOADER_RUN_COLS)}) "
                f"VALUES ({','.join('%(' + c + ')s' for c in LOADER_RUN_COLS)}) "
                f"ON CONFLICT (run_id) DO NOTHING",
                {c: row.get(c) for c in LOADER_RUN_COLS})
        return True
    except Exception as e:                      # noqa: BLE001 - bookkeeping, not the job
        print(f"loader_run not recorded: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--tempo", required=True); ap.add_argument("--dsn", required=True)
    ap.add_argument("--since", default="24h"); a = ap.parse_args(argv)
    import requests
    hours = int(a.since.rstrip("h")); end = int(dt.datetime.now().timestamp()); start = end - hours * 3600
    if hours < MIN_SAFE_WINDOW_HOURS:
        print(f"stdtel: --since {a.since} is narrower than Tempo's flush delay. A span whose "
              f"timestamp is in the past — every sub-agent activation is, because it carries the "
              f"sub-agent's own start and end — is not searchable until the ingester flushes its "
              f"block, up to {TEMPO_FLUSH_MINUTES} minutes later. A window this narrow will step "
              f"over those spans and never come back for them. Use {MIN_SAFE_WINDOW_HOURS}h or more; "
              f"rows are keyed on span_id, so overlapping runs cost nothing.", file=sys.stderr)
    run = {"run_id": uuid.uuid4().hex, "loader": LOADER,
           "started_at": dt.datetime.now(dt.timezone.utc), "finished_at": None,
           "rows_loaded": 0, "source_max_ts": None, "ok": False, "error": None}

    def search(query):
        # Tempo returns nothing at all without start/end — it looks like a dead pipeline
        r = requests.get(f"{a.tempo}/api/search",
                         params={"q": query, "start": start, "end": end, "limit": 1000})
        r.raise_for_status()
        return r.json().get("traces", [])

    try:
        traces = {}
        for span_name in (SPAN_NAME, LEGACY_SKILL_SPAN_NAME, SESSION_SPAN_NAME):
            traces.update({t["traceID"]: t for t in search(f'{{ name = "{span_name}" }}')})
        activations, rows, session_rows = collect(
            traces, lambda tid: requests.get(f"{a.tempo}/api/traces/{tid}").json())
        run["source_max_ts"] = max((r["started_at"] for r in activations + session_rows),
                                  default=None)
        run["rows_loaded"] = write(a.dsn, [
            ("artefact_activation", ACTIVATION_COLS, activations, "span_id"),
            ("skill_invocation", COLS, rows, "span_id"),
            ("session_cost", SESSION_COLS, session_rows, "session_id"),
        ])
        run["ok"] = True
    except Exception as e:                      # noqa: BLE001 - every run writes a row
        run["error"] = f"{type(e).__name__}: {e}"[:2000]
        run["finished_at"] = dt.datetime.now(dt.timezone.utc)
        record_run(a.dsn, run)
        print(f"load failed: {run['error']}", file=sys.stderr)
        return 1
    run["finished_at"] = dt.datetime.now(dt.timezone.utc)
    record_run(a.dsn, run)
    print(f"loaded {len(activations)} activation(s) "
          f"({len(rows)} of kind skill, also written to skill_invocation), "
          f"{len(session_rows)} session cost row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
