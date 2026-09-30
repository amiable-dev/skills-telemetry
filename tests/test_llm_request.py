"""ADR-014 decisions 5 and 7: one table of model requests, loaded from the harness's
own records, joined to change requests through the turn (#112).

The fixture is a real Loki response, recorded 2026-09-30 from Claude Code 2.1.285
through this repository's collector: a bare prompt, and `/probe-echo`, a project
skill. It is what proved the attribution is on the event — `api_request` carried
`skill_name=probe-echo` while `skill_activated` said only `custom_skill`. Names
are as Loki stores them, dots flattened to underscores; reading `skill.name`
would load a column of NULLs.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "loki_claude_code.json").read_text())
VALUES = FIXTURE["data"]["result"][0]["values"]
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
PREFIX = "wtest-"


def loader():
    spec = importlib.util.spec_from_file_location("load_requests_t", ROOT / "warehouse" / "load_requests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _api(values=VALUES):
    return [v for v in values if v[2]["structuredMetadata"]["event_name"] == "api_request"]


# --- parsing the recorded events -------------------------------------------------------------

def test_only_api_requests_become_rows():
    rows, rejected = loader().parse_values(VALUES)
    assert len(VALUES) == 5 and len(rows) == 2 and rejected == 0


def test_a_recorded_request_round_trips_exactly():
    rows, _ = loader().parse_values(VALUES)
    first = rows[0]
    meta = _api()[0][2]["structuredMetadata"]
    assert first["harness"] == "claude-code"
    assert first["attribution_source"] == "native"
    assert first["request_id"] == meta["request_id"] and first["request_id"].startswith(PREFIX)
    assert first["session_id"] == meta["session_id"] and first["prompt_id"] == meta["prompt_id"]
    assert first["model"] == "claude-haiku-4-5-20251001"
    assert (first["input_tokens"], first["output_tokens"]) == (int(meta["input_tokens"]), int(meta["output_tokens"]))
    assert first["cache_read_tokens"] == int(meta["cache_read_tokens"])
    assert first["cache_creation_tokens"] == int(meta["cache_creation_tokens"])
    assert first["duration_ms"] == int(meta["duration_ms"])
    assert first["query_source"] == "sdk"
    assert first["user_hash"] == "wtest-user-hash"
    assert first["ended_at"] == dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
        microseconds=(int(_api()[0][0]) - 915148800 * 10**9) // 1000)


def test_cost_keeps_every_decimal_the_harness_sent():
    """0.0425119 in NUMERIC(12,4) is 0.0425: per request that is noise, summed
    over a month it is the drift the reconciliation would then report."""
    rows, _ = loader().parse_values(VALUES)
    sent = [Decimal(v[2]["structuredMetadata"]["cost_usd"]) for v in _api()]
    assert [r["cost_usd"] for r in rows] == sent
    assert any(c.as_tuple().exponent < -4 for c in sent), "the fixture must exercise sub-4dp precision"
    assert "cost_usd NUMERIC," in re.sub(r"\s+", " ", _table_body("llm_request")) or \
           re.search(r"cost_usd\s+NUMERIC\s*(,|\(\s*\d+\s*,\s*([7-9]|\d\d)\s*\))", _table_body("llm_request"))


def test_the_skill_is_read_from_the_request_itself():
    rows, _ = loader().parse_values(VALUES)
    assert [r["skill_name"] for r in rows] == [None, "probe-echo"]


def test_absent_attribution_is_null_never_empty():
    """ADR-005: an unobserved value is not recorded."""
    rows, _ = loader().parse_values(VALUES)
    for col in ("agent_name", "plugin_name", "mcp_server", "mcp_tool", "branch_hash"):
        assert all(r[col] is None for r in rows), col
    assert rows[0]["skill_name"] is None


def test_every_attribution_attribute_is_read_under_lokis_name():
    meta = dict(_api()[0][2]["structuredMetadata"])
    meta.update({"agent_name": "Explore", "plugin_name": "stdtel", "mcp_server_name": "llm-council",
                 "mcp_tool_name": "verify", "skill_name": "third-party"})
    rows, _ = loader().parse_values([[_api()[0][0], "claude_code.api_request", {"structuredMetadata": meta}]])
    r = rows[0]
    assert (r["agent_name"], r["plugin_name"], r["mcp_server"], r["mcp_tool"], r["skill_name"]) == \
        ("Explore", "stdtel", "llm-council", "verify", "third-party")


def test_a_request_without_an_id_is_counted_not_silently_dropped():
    meta = {k: v for k, v in _api()[0][2]["structuredMetadata"].items() if k != "request_id"}
    rows, rejected = loader().parse_values([[_api()[0][0], "x", {"structuredMetadata": meta}]])
    assert rows == [] and rejected == 1


def test_a_run_that_rejected_events_fails_and_says_how_many():
    """Loading the rest is right; reporting success is not."""
    mod = loader()
    meta = {k: v for k, v in _api()[0][2]["structuredMetadata"].items() if k != "request_id"}
    page = [[_api()[0][0], "x", {"structuredMetadata": meta}], _api()[1]]
    recorded, written = [], []
    mod.fetch_page_from_loki = lambda base: (lambda s, e, n: page[:n] if s <= int(page[0][0]) else [])
    mod.write = lambda dsn, batches: written.extend(batches[0][2]) or len(batches[0][2])
    mod.record_run = lambda dsn, run: recorded.append(run) or True
    assert mod.main(["--loki", "x", "--dsn", "y", "--start-ns", "0"]) == 1
    assert len(written) == 1
    assert recorded[0]["ok"] is False and "1 api_request event(s) had no request_id" in recorded[0]["error"]


def test_a_missing_cost_is_null_not_zero():
    meta = {k: v for k, v in _api()[0][2]["structuredMetadata"].items() if k != "cost_usd"}
    rows, _ = loader().parse_values([[_api()[0][0], "x", {"structuredMetadata": meta}]])
    assert rows[0]["cost_usd"] is None


def test_a_probe_is_never_a_row():
    meta = dict(_api()[0][2]["structuredMetadata"], service_name="stdtel-probe")
    rows, _ = loader().parse_values([[_api()[0][0], "x", {"structuredMetadata": meta}]])
    assert rows == []


# --- reading all of it: Loki truncates at `limit` and says nothing --------------------------

def test_pagination_reads_past_the_limit_and_does_not_double_count():
    mod = loader()
    events = [[str(1000 + i), "e", {"structuredMetadata": {"request_id": f"r{i}"}}] for i in range(12)]
    events.insert(5, [events[4][0], "e", {"structuredMetadata": {"request_id": "r4-twin"}}])  # same ns
    calls = []

    def page(start_ns, end_ns, limit):
        calls.append(start_ns)
        return [e for e in events if start_ns <= int(e[0]) < end_ns][:limit]

    got = mod.fetch_all(page, start_ns=0, end_ns=10**6, limit=5)
    ids = [e[2]["structuredMetadata"]["request_id"] for e in got]
    assert sorted(ids) == sorted(e[2]["structuredMetadata"]["request_id"] for e in events)
    assert len(ids) == len(set(ids)) == 13
    assert len(calls) > 2


def test_pagination_refuses_to_spin_on_one_timestamp():
    mod = loader()
    events = [["1000", "e", {"structuredMetadata": {"request_id": f"r{i}"}}] for i in range(6)]
    with pytest.raises(RuntimeError, match="same nanosecond"):
        mod.fetch_all(lambda s, e, n: [x for x in events if s <= int(x[0]) < e][:n], 0, 10**6, 5)


def test_the_query_asks_for_categorised_labels_and_api_requests_only():
    mod = loader()
    assert 'event_name="api_request"' in mod.QUERY and 'service_name="claude-code"' in mod.QUERY
    assert mod.HEADERS.get("X-Loki-Response-Encoding-Flags") == "categorize-labels"


# --- schema contract ---------------------------------------------------------------------------

def _table_body(table: str) -> str:
    return re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.S).group(1)


def test_every_loader_column_exists_in_the_schema():
    from tests.test_warehouse import schema_columns
    mod = loader()
    assert set(mod.REQUEST_COLS) <= set(schema_columns("llm_request"))
    rows, _ = mod.parse_values(VALUES)
    assert set(rows[0]) == set(mod.REQUEST_COLS)


def test_make_load_runs_the_request_loader_against_loki():
    mk = (ROOT / "Makefile").read_text()
    load = re.search(r"^load:.*$", mk, re.M).group(0)
    assert "warehouse.load_requests" in load
    assert "STDTEL_LOKI" in mk


def test_the_compose_loader_runs_it_and_its_arguments_parse(monkeypatch):
    """#121: the compose loader ran load_traces with no arguments, which argparse
    rejected on every cycle. Here the environment the service sets is enough."""
    import os
    import shlex
    import yaml
    svc = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())["services"]["loader"]
    command = " ".join(str(x) for x in svc["command"])
    call = re.search(r"python -m warehouse\.load_requests([^\n|]*)", command)
    assert call, "the loader service must run load_requests"
    assert svc["environment"]["STDTEL_LOKI"] == "http://loki:3100"
    assert "loki" in svc["depends_on"]
    for k, v in svc["environment"].items():
        monkeypatch.setenv(k, str(v))
    argv = [os.path.expandvars(w) for w in shlex.split(call.group(1).replace("$$", "$"))]
    a = loader().parse_args(argv)
    assert (a.loki, a.dsn) == ("http://loki:3100", svc["environment"]["STDTEL_DSN"])


# --- against a real Postgres -----------------------------------------------------------------

def _connect():
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed")
    try:
        return psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:                           # noqa: BLE001
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")


def _clean(cur):
    for table, col in (("llm_request", "request_id"), ("artefact_activation", "span_id"),
                       ("change_request", "cr_id"), ("session_cost", "session_id"),
                       ("loader_run", "run_id")):
        cur.execute(f"DELETE FROM {table} WHERE {col} LIKE %s", (PREFIX + "%",))


def q(sql, params=None):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.fixture(scope="module")
def warehouse():
    conn = _connect()
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        _clean(cur)
    conn.close()
    mod = loader()
    run_id = PREFIX + "run-requests"
    real = mod.fetch_page_from_loki, mod.new_run_id
    mod.fetch_page_from_loki = lambda base: (lambda s, e, n: [v for v in _api() if s <= int(v[0]) < e][:n])
    mod.new_run_id = lambda: run_id
    try:
        assert mod.main(["--loki", "http://wtest.invalid", "--dsn", DSN, "--since", "24h",
                         "--start-ns", "915148800000000000"]) == 0
    finally:
        mod.fetch_page_from_loki, mod.new_run_id = real
    rows, _ = mod.parse_values(VALUES)
    skill_req = rows[1]
    t0 = skill_req["ended_at"]
    with _connect() as conn, conn.cursor() as cur:
        ins = ("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, ended_at, kind, "
               "prompt_id, branch_hash, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'hook')")
        # two turn deltas for one prompt (ADR-009), and a sub-agent sharing the prompt id: the
        # fan-out that inflated n_with 76x in #23
        for span, kind, branch, lead in (("wtest-turn-a", "turn", "wtest-branch", 5),
                                         ("wtest-turn-b", "turn", "wtest-branch", 4),
                                         # a sub-agent in its own worktree: another branch, another
                                         # change request, the same prompt id, and it started first
                                         ("wtest-sub", "subagent", "wtest-other-branch", 9)):
            cur.execute(ins, (span, "wtest-trace", skill_req["session_id"], t0 - dt.timedelta(seconds=lead),
                              t0 + dt.timedelta(seconds=1), kind, skill_req["prompt_id"], branch))
        cur.execute("INSERT INTO change_request (cr_id, forge, repo_id, number, branch_hash, opened_at, "
                    "closed_at, state) VALUES ('wtest-cr-other', 'github', 'wtest/repo', 2, "
                    "'wtest-other-branch', %s, %s, 'merged')", (t0 - dt.timedelta(days=1), t0 + dt.timedelta(days=1)))
        cur.execute("INSERT INTO change_request (cr_id, forge, repo_id, number, branch_hash, opened_at, "
                    "closed_at, state) VALUES ('wtest-cr', 'github', 'wtest/repo', 1, 'wtest-branch', %s, %s, "
                    "'merged')", (t0 - dt.timedelta(days=1), t0 + dt.timedelta(days=1)))
        cur.execute("INSERT INTO session_cost (session_id, harness, cost_usd) VALUES (%s, 'claude-code', 1.5)",
                    (skill_req["session_id"],))
    yield {"rows": rows, "run_id": run_id}
    conn = _connect()
    with conn, conn.cursor() as cur:
        _clean(cur)
    conn.close()


def test_requests_land_with_their_precision(warehouse):
    got = q("SELECT request_id, cost_usd, skill_name, attribution_source FROM llm_request "
            "WHERE request_id LIKE 'wtest-%%' ORDER BY ended_at")
    assert [(g["cost_usd"], g["skill_name"]) for g in got] == \
        [(r["cost_usd"], r["skill_name"]) for r in warehouse["rows"]]
    assert {g["attribution_source"] for g in got} == {"native"}


def test_the_run_is_recorded(warehouse):
    run = q("SELECT loader, ok, rows_loaded FROM loader_run WHERE run_id = %s", (warehouse["run_id"],))
    assert run == [{"loader": "load_requests", "ok": True, "rows_loaded": 2}]


def test_loading_twice_changes_nothing(warehouse):
    mod = loader()
    mod.fetch_page_from_loki = lambda base: (lambda s, e, n: [v for v in _api() if s <= int(v[0]) < e][:n])
    mod.new_run_id = lambda: PREFIX + "run-requests-2"
    assert mod.main(["--loki", "x", "--dsn", DSN, "--start-ns", "915148800000000000"]) == 0
    assert q("SELECT count(*) AS n FROM llm_request WHERE request_id LIKE 'wtest-%%'") == [{"n": 2}]


def test_a_request_joins_its_change_request_through_the_turn_once(warehouse):
    """Two turn deltas and a sub-agent carry the same prompt id. Each request
    must still appear once, or cost is multiplied (#23), and the turn decides:
    decision 7 names the turn as the bridge, not whichever span started first."""
    skill_req = warehouse["rows"][1]
    got = q("SELECT request_id, cr_id, method FROM llm_request_change_request WHERE request_id = %s",
            (skill_req["request_id"],))
    assert got == [{"request_id": skill_req["request_id"], "cr_id": "wtest-cr", "method": "branch"}]
    total = q("SELECT sum(r.cost_usd) AS s FROM llm_request_change_request v "
              "JOIN llm_request r USING (harness, request_id) WHERE v.cr_id = 'wtest-cr'")
    assert total == [{"s": skill_req["cost_usd"]}]


def test_a_request_with_no_turn_has_no_change_request(warehouse):
    bare = warehouse["rows"][0]
    assert q("SELECT 1 FROM llm_request_change_request WHERE request_id = %s", (bare["request_id"],)) == []


def test_the_view_reuses_adr_013s_rule_rather_than_restating_it():
    view = re.search(r"CREATE OR REPLACE VIEW llm_request_change_request AS(.*?);", SCHEMA, re.S).group(1)
    assert "activation_change_request" in view
    assert "30 days" not in view and "change_request_commit" not in view


def test_session_cost_is_only_a_reconciliation(warehouse):
    """Decision 5: the harness's cumulative total is compared with the sum of
    requests, never added to it."""
    sid = warehouse["rows"][1]["session_id"]
    got = q("SELECT session_total_usd, requests_usd, requests FROM session_cost_reconciliation "
            "WHERE session_id = %s", (sid,))
    per_session = sum(r["cost_usd"] for r in warehouse["rows"] if r["session_id"] == sid)
    n = sum(1 for r in warehouse["rows"] if r["session_id"] == sid)
    assert got == [{"session_total_usd": Decimal("1.5"), "requests_usd": per_session, "requests": n}]
