"""The ADR-009 warehouse half, against a real Postgres.

The loader's parsing is unit-tested in `test_warehouse.py`; these tests are the
half that only shows when a query meets rows — the same lesson as #23, where
`scorecard.sql` reported n_with = 3192 against 120 PRs the first time it was
ever executed. Here that means: every new column survives the round trip, a
kind=skill activation reaches BOTH tables, a pre-ADR-009 span still loads, a
`loader_run` row is written whether the run worked or not, and each of the five
efficiency queries parses, runs, and returns the shape its header promises.

Every id written here starts with `wtest-`, and every timestamp is in 1999, so
nothing can collide with a real row or a `demo-` one — and the efficiency
queries, which are filtered by time rather than by session, cannot accidentally
read production rows into an assertion. The teardown deletes that prefix and
nothing else.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
PREFIX = "wtest-"
#: A window no real telemetry can fall into, so the time-filtered queries see
#: only what this module seeded.
SINCE = dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(1999, 1, 2, tzinfo=dt.timezone.utc)
SESSION = PREFIX + "sess-1"
OTHER_SESSION = PREFIX + "sess-2"


def _connect():
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed (pip install -e '.[warehouse]')")
    try:
        return psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:                       # noqa: BLE001 - environment, not logic
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")


def loader():
    spec = importlib.util.spec_from_file_location(
        "load_traces_it", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- building Tempo's OTLP JSON shape, which is what the loader actually reads ---

def _kv(attrs: dict) -> list[dict]:
    out = []
    for k, v in attrs.items():
        if isinstance(v, bool):
            value = {"boolValue": v}
        elif isinstance(v, int):
            value = {"intValue": str(v)}
        elif isinstance(v, float):
            value = {"doubleValue": v}
        else:
            value = {"stringValue": str(v)}
        out.append({"key": k, "value": value})
    return out


def otlp_span(name: str, span_id: str, attrs: dict, *, start: dt.datetime,
              seconds: int = 60) -> dict:
    return {
        "name": name,
        "spanID": span_id,
        "traceID": PREFIX + "trace",
        "startTimeUnixNano": str(int(start.timestamp() * 1e9)),
        "endTimeUnixNano": str(int((start + dt.timedelta(seconds=seconds)).timestamp() * 1e9)),
        "attributes": _kv(attrs),
    }


RESOURCE = {"std.harness": "claude-code", "std.harness.mode": "agent",
            "std.ticket.id": "WTEST-1", "std.repo": "wtest-repo", "std.team": "wtest-team",
            "std.user.hash": "wtest-user-a"}


def trace_doc(spans: list[dict], resource: dict | None = None) -> dict:
    return {"batches": [{"resource": {"attributes": _kv(resource or RESOURCE)},
                         "scopeSpans": [{"spans": spans}]}]}


def at(minutes: int) -> dt.datetime:
    return SINCE + dt.timedelta(minutes=minutes)


# --- the fleet this module writes ---

def seed_spans() -> list[dict]:
    mod = loader()
    usage = {"gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 800,
             "gen_ai.usage.cache_read_input_tokens": 90000,
             "gen_ai.usage.cache_creation_input_tokens": 4000}
    spans = [
        # a skill, with every contract field the kind may carry
        otlp_span(mod.SPAN_NAME, PREFIX + "act-skill", {
            "session.id": SESSION, "std.artefact.kind": "skill",
            "std.artefact.name": "structured-logging", "std.artefact.source": "hook",
            "std.skill.name": "structured-logging",
            "std.skill.invoked_as": "epic-loop:structured-logging",
            "std.skill.plugin": "epic-loop", "std.skill.version": "2.3.0",
            "std.skill.content_hash": "wtesthash", "std.standard_id": "STD-LOG-001",
            "std.policy.ids": "logging.required_fields,logging.no_pii",
            "std.skill.trigger": "direct", "std.skill.load_tokens": 7,
            "std.skill.tail_tokens": 28199, "std.skill.tail_tokens_first_only": 28199,
            "std.skill.llm_requests": 11, "std.skill.duration_ms": 4300,
            "std.prompt.id": PREFIX + "prompt-1",
            "std.harness.permission_mode": "acceptEdits",
            "gen_ai.request.model": "claude-opus-5", **usage,
        }, start=at(1)),
        # two Stops inside one turn: two rows, one turn
        otlp_span(mod.SPAN_NAME, PREFIX + "act-turn-1a", {
            "session.id": SESSION, "std.artefact.kind": "turn",
            "std.artefact.source": "transcript", "std.prompt.id": PREFIX + "prompt-1",
            "std.turn.llm_requests": 6, "std.turn.tool_calls": 14,
            "std.turn.duration_ms": 120000, "std.turn.hook_ms": 312,
            "std.turn.hook.stdtel-hook.ms": 12, "std.turn.hook.format-sh.ms": 300,
            "gen_ai.request.model": "claude-opus-5",
            "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 600,
            "gen_ai.usage.cache_read_input_tokens": 70000,
            "gen_ai.usage.cache_creation_input_tokens": 3000,
        }, start=at(2)),
        otlp_span(mod.SPAN_NAME, PREFIX + "act-turn-1b", {
            "session.id": SESSION, "std.artefact.kind": "turn",
            "std.artefact.source": "transcript", "std.prompt.id": PREFIX + "prompt-1",
            "std.turn.llm_requests": 2, "std.turn.hook_ms": 20,
            "std.turn.hook.stdtel-hook.ms": 20,
            "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50,
        }, start=at(3)),
        otlp_span(mod.SPAN_NAME, PREFIX + "act-turn-2", {
            "session.id": SESSION, "std.artefact.kind": "turn",
            "std.artefact.source": "transcript", "std.prompt.id": PREFIX + "prompt-2",
            "std.turn.llm_requests": 4, "std.turn.hook_ms": 15,
            "std.turn.hook.stdtel-hook.ms": 15,
            "gen_ai.usage.input_tokens": 300, "gen_ai.usage.output_tokens": 200,
        }, start=at(20)),
        # two sub-agent calls of one type, one of another, one with no usage at all
        otlp_span(mod.SPAN_NAME, PREFIX + "act-sub-1", {
            "session.id": SESSION, "std.artefact.kind": "subagent",
            "std.artefact.name": "Explore", "std.artefact.source": "hook",
            "std.subagent.type": "Explore", "std.subagent.id": PREFIX + "agent-1",
            "std.subagent.depth": 1, "std.subagent.llm_requests": 9,
            "std.subagent.tool_calls": 31, "std.subagent.duration_ms": 65000,
            "std.artefact.parent_prompt_id": PREFIX + "prompt-1",
            "gen_ai.request.model": "claude-haiku-5",
            "gen_ai.usage.input_tokens": 400, "gen_ai.usage.output_tokens": 2200,
            "gen_ai.usage.cache_read_input_tokens": 3200000,
            "gen_ai.usage.cache_creation_input_tokens": 12000,
        }, start=at(5)),
        otlp_span(mod.SPAN_NAME, PREFIX + "act-sub-2", {
            "session.id": SESSION, "std.artefact.kind": "subagent",
            "std.artefact.name": "Explore", "std.artefact.source": "transcript",
            "std.subagent.type": "Explore", "std.subagent.id": PREFIX + "agent-2",
            "std.subagent.llm_requests": 3, "std.subagent.tool_calls": 8,
            "std.artefact.parent_prompt_id": PREFIX + "prompt-2",
            "gen_ai.usage.input_tokens": 200, "gen_ai.usage.output_tokens": 900,
            "gen_ai.usage.cache_read_input_tokens": 500000,
            "gen_ai.usage.cache_creation_input_tokens": 3000,
        }, start=at(6)),
        otlp_span(mod.SPAN_NAME, PREFIX + "act-sub-3", {
            "session.id": SESSION, "std.artefact.kind": "subagent",
            "std.artefact.name": "general-purpose", "std.artefact.source": "hook",
            "std.subagent.type": "general-purpose", "std.subagent.id": PREFIX + "agent-3",
        }, start=at(7)),
        # a compaction, after those turns and sub-agents
        otlp_span(mod.SPAN_NAME, PREFIX + "act-compact", {
            "session.id": SESSION, "std.artefact.kind": "compaction",
            "std.artefact.name": "auto", "std.artefact.source": "hook",
            "std.compaction.reason": "auto", "std.compaction.tokens_before": 954000,
            "std.compaction.tokens_after": 31000,
            "std.compaction.turns_since_previous": 44,
        }, start=at(10)),
        # a pre-ADR-009 span: no std.artefact.* at all
        otlp_span(mod.LEGACY_SKILL_SPAN_NAME, PREFIX + "act-legacy", {
            "session.id": OTHER_SESSION, "std.skill.name": "stdtel-onboard",
            "std.skill.version": "1.0.0", "std.skill.llm_requests": 2,
            "std.skill.load_tokens": 900, "std.skill.tail_tokens": 4100,
            "gen_ai.usage.input_tokens": 500, "gen_ai.usage.output_tokens": 300,
            "gen_ai.usage.cache_creation_input_tokens": 1000,
            "gen_ai.usage.cache_read_input_tokens": 20000,
        }, start=at(30)),
        # the session total, carrying real money and the harness's own wall time
        otlp_span(mod.SESSION_SPAN_NAME, PREFIX + "session-span", {
            "session.id": SESSION, "std.session.llm_requests": 12,
            "std.session.cost_usd": 366.42, "std.session.api_ms": 1234567,
            "std.session.tool_ms": 76543, "std.session.duration_ms": 9876543,
            "gen_ai.request.model": "claude-opus-5",
            "gen_ai.usage.input_tokens": 5000, "gen_ai.usage.output_tokens": 4000,
        }, start=at(0), seconds=3600),
    ]
    return spans


def fake_requests(spans: list[dict]):
    """A stand-in for Tempo: search returns one trace id, /api/traces returns it."""
    class _R:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def get(url, params=None, **kw):
        if "/api/search" in url:
            return _R({"traces": [{"traceID": PREFIX + "trace"}]})
        return _R(trace_doc(spans))
    return get


def _delete_wtest_rows(cur) -> None:
    """Scoped to this module's prefix, and to nothing else.

    An unscoped DELETE or UPDATE here would corrupt the warehouse these tests
    exist to check — it has happened once already, on eight real rows.
    """
    for table, column in (("artefact_activation", "session_id"), ("artefact_activation", "span_id"),
                          ("skill_invocation", "session_id"), ("skill_invocation", "span_id"),
                          ("session_cost", "session_id"), ("loader_run", "run_id")):
        # session_id as well as span_id: the loader rewrites ids it recognises as
        # OTLP base64, so scoping on span_id alone once left rows behind
        cur.execute(f"DELETE FROM {table} WHERE {column} LIKE %s", (PREFIX + "%",))


@pytest.fixture(scope="module")
def warehouse():
    """Apply the schema (idempotent), run the loader over a fake Tempo, clean up."""
    conn = _connect()
    with conn, conn.cursor() as cur:
        cur.execute((ROOT / "warehouse" / "schema.sql").read_text())
        _delete_wtest_rows(cur)
    conn.close()
    yield
    conn = _connect()
    with conn, conn.cursor() as cur:
        _delete_wtest_rows(cur)
    conn.close()


@pytest.fixture(scope="module")
def loaded(warehouse, request):
    """One real `main()` run: search, parse, insert, and the loader_run row."""
    import requests
    mod = loader()
    run_id = PREFIX + "run-ok"

    class _U:
        hex = run_id

    real_get, real_uuid4 = requests.get, mod.uuid.uuid4
    requests.get = fake_requests(seed_spans())
    mod.uuid.uuid4 = lambda: _U()
    try:
        rc = mod.main(["--tempo", "http://wtest.invalid", "--dsn", DSN, "--since", "24h"])
    finally:
        requests.get, mod.uuid.uuid4 = real_get, real_uuid4
    assert rc == 0
    return run_id


def rows(sql: str, params=None) -> list[dict]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def efficiency(name: str, params: dict) -> list[dict]:
    sql = (ROOT / "warehouse" / "efficiency" / name).read_text()
    for key in params:
        sql = sql.replace(f":{key}", f"%({key})s")
    return rows(sql, params)


# --- the round trip ---

def test_every_kind_reaches_the_activation_table(loaded):
    got = {r["kind"]: r["n"] for r in rows(
        "SELECT kind, count(*) AS n FROM artefact_activation "
        "WHERE span_id LIKE %(p)s GROUP BY kind", {"p": PREFIX + "%"})}
    assert got == {"skill": 2, "turn": 3, "subagent": 3, "compaction": 1}, got


def test_a_skill_activation_lands_in_both_tables(loaded):
    """ADR-009's compatibility decision: one span, two tables, so scorecard.sql
    and every pre-ADR row keep working with no dual emission on the wire."""
    act = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
               {"s": PREFIX + "act-skill"})
    inv = rows("SELECT * FROM skill_invocation WHERE span_id = %(s)s",
               {"s": PREFIX + "act-skill"})
    assert len(act) == 1 and len(inv) == 1, "a skill activation must reach both tables"
    assert act[0]["kind"] == "skill" and act[0]["name"] == "structured-logging"
    assert inv[0]["skill_name"] == "structured-logging"
    assert inv[0]["plugin"] == "epic-loop" and inv[0]["tail_tokens"] == 28199
    assert inv[0]["policy_ids"] == ["logging.required_fields", "logging.no_pii"]


def test_only_skills_reach_skill_invocation(loaded):
    """A sub-agent is not a skill. The primary metric reads kind=skill and
    nothing else, and a turn row in skill_invocation would change its cohorts."""
    other = rows("SELECT span_id FROM skill_invocation WHERE span_id LIKE %(p)s "
                 "AND span_id NOT IN (%(a)s, %(b)s)",
                 {"p": PREFIX + "%", "a": PREFIX + "act-skill", "b": PREFIX + "act-legacy"})
    assert other == [], other


def test_a_legacy_skill_invocation_span_still_loads(loaded):
    """Rows recorded before the rename are still in Tempo and carry no kind."""
    act = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
               {"s": PREFIX + "act-legacy"})
    inv = rows("SELECT * FROM skill_invocation WHERE span_id = %(s)s",
               {"s": PREFIX + "act-legacy"})
    assert len(act) == 1 and act[0]["kind"] == "skill"
    assert act[0]["name"] == "stdtel-onboard"
    assert len(inv) == 1 and inv[0]["skill_name"] == "stdtel-onboard"


def test_every_new_column_round_trips(loaded):
    sub = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
               {"s": PREFIX + "act-sub-1"})[0]
    assert sub["subagent_type"] == "Explore"
    assert sub["subagent_id"] == PREFIX + "agent-1"
    assert sub["subagent_depth"] == 1
    assert sub["parent_prompt_id"] == PREFIX + "prompt-1"
    assert sub["llm_requests"] == 9 and sub["tool_calls"] == 31
    assert sub["duration_ms"] == 65000
    assert sub["cache_read_tokens"] == 3200000
    assert sub["source"] == "hook" and sub["model"] == "claude-haiku-5"
    assert sub["ticket_id"] == "WTEST-1" and sub["team"] == "wtest-team"
    assert sub["user_hash"] == "wtest-user-a" and sub["harness"] == "claude-code"

    comp = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
                {"s": PREFIX + "act-compact"})[0]
    assert comp["compaction_reason"] == "auto"
    assert comp["compaction_tokens_before"] == 954000
    assert comp["compaction_tokens_after"] == 31000
    assert comp["compaction_turns_since_previous"] == 44

    turn = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
                {"s": PREFIX + "act-turn-1a"})[0]
    assert turn["name"] is None, "a turn carries no name: prompt_id is unbounded"
    assert turn["prompt_id"] == PREFIX + "prompt-1"
    assert turn["hook_ms"] == 312
    assert turn["hook_ms_by_hook"] == {"format-sh": 300, "stdtel-hook": 12}


def test_an_unobserved_value_is_null_in_the_database_too(loaded):
    """ADR-005 has to survive the driver as well as the parser."""
    bare = rows("SELECT * FROM artefact_activation WHERE span_id = %(s)s",
                {"s": PREFIX + "act-sub-3"})[0]
    for column in ("input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_creation_tokens", "llm_requests", "tool_calls",
                   "duration_ms", "hook_ms", "hook_ms_by_hook", "subagent_depth"):
        assert bare[column] is None, f"{column} was invented for an unobserved value"


def test_session_cost_carries_money_and_wall_time(loaded):
    row = rows("SELECT * FROM session_cost WHERE session_id = %(s)s", {"s": SESSION})
    assert len(row) == 1
    assert float(row[0]["cost_usd"]) == 366.42
    assert row[0]["api_ms"] == 1234567
    assert row[0]["tool_ms"] == 76543
    assert row[0]["duration_ms"] == 9876543


# --- loader_run: a loader that records only its successes is a loader nobody checks ---

def test_loader_run_is_written_on_success(loaded):
    run = rows("SELECT * FROM loader_run WHERE run_id = %(r)s", {"r": loaded})
    assert len(run) == 1, "a successful run must leave a row"
    assert run[0]["ok"] is True and run[0]["error"] is None
    assert run[0]["loader"] == "load_traces"
    assert run[0]["rows_loaded"] >= 10
    assert run[0]["finished_at"] >= run[0]["started_at"]
    # the lag measurement: the newest source timestamp the run actually saw
    assert run[0]["source_max_ts"] is not None
    assert SINCE <= run[0]["source_max_ts"] < UNTIL


def test_loader_run_is_written_on_failure(warehouse):
    """The run that fails is the one you most need a row for."""
    import requests
    mod = loader()
    run_id = PREFIX + "run-fail"

    class _U:
        hex = run_id

    def boom(*a, **kw):
        raise RuntimeError("tempo unreachable")

    real_get, real_uuid4 = requests.get, mod.uuid.uuid4
    requests.get = boom
    mod.uuid.uuid4 = lambda: _U()
    try:
        rc = mod.main(["--tempo", "http://wtest.invalid", "--dsn", DSN])
    finally:
        requests.get, mod.uuid.uuid4 = real_get, real_uuid4
    assert rc == 1, "a failed load must not report success"
    run = rows("SELECT * FROM loader_run WHERE run_id = %(r)s", {"r": run_id})
    assert len(run) == 1, "a failed run must leave a row too"
    assert run[0]["ok"] is False
    assert "tempo unreachable" in run[0]["error"]
    assert run[0]["rows_loaded"] == 0


# --- the five named efficiency questions (ADR-009 decision 8) ---

def test_tokens_by_artefact_kind_reports_every_kind_and_counts_turns_once(loaded):
    out = {r["kind"]: r for r in efficiency("01_tokens_by_artefact_kind.sql",
                                            {"session_id": SESSION})}
    assert set(out) == {"skill", "turn", "subagent", "compaction"}, out
    assert all(r["n_rows"] == 4 for r in out.values()), "each row reports the row count"
    # three turn rows, two turns: the second Stop of prompt-1 must not be a turn
    assert out["turn"]["n_activations"] == 3
    assert out["turn"]["n_turns"] == 2
    assert out["turn"]["input_tokens"] == 900 + 100 + 300
    assert out["subagent"]["n_without_usage"] == 1, "the usage-free sub-agent is declared"
    assert out["subagent"]["cache_read_tokens"] == 3200000 + 500000
    assert out["compaction"]["input_tokens"] is None, "a compaction reports no usage"


def test_tokens_by_kind_is_scoped_to_the_session_it_was_asked_about(loaded):
    out = efficiency("01_tokens_by_artefact_kind.sql", {"session_id": OTHER_SESSION})
    assert len(out) == 1 and out[0]["kind"] == "skill"
    assert out[0]["n_activations"] == 1


def test_subagent_cost_per_call_groups_by_type(loaded):
    out = {r["subagent_type"]: r for r in efficiency(
        "02_subagent_cost_per_call.sql", {"since": SINCE, "until": UNTIL})}
    assert set(out) == {"Explore", "general-purpose"}, out
    assert out["Explore"]["n_calls"] == 2
    assert out["Explore"]["n_sessions"] == 1
    assert out["Explore"]["n_parent_turns"] == 2
    assert out["Explore"]["n_inferred_from_transcript"] == 1
    assert out["Explore"]["output_tokens"] == 2200 + 900
    assert out["Explore"]["p50_cache_read_tokens"] == pytest.approx(1850000)
    assert out["general-purpose"]["n_calls_without_usage"] == 1
    assert out["general-purpose"]["output_tokens"] is None


def test_compaction_frequency_reports_what_preceded_it(loaded):
    out = efficiency("03_compaction_frequency.sql", {"since": SINCE, "until": UNTIL})
    assert len(out) == 1, out
    row = out[0]
    assert row["n_rows"] == 1 and row["n_compactions_in_session"] == 1
    assert row["compaction_reason"] == "auto"
    assert row["tokens_dropped"] == 954000 - 31000
    assert row["minutes_since_previous"] is None, "no previous compaction in the window"
    # before it: turn-1a and turn-1b (one turn), three sub-agents, one skill
    assert row["n_turns_before"] == 1
    assert row["n_subagent_calls_before"] == 3
    assert row["n_skill_loads_before"] == 1
    assert row["turn_tokens_before"] == 900 + 600 + 100 + 50


def test_hook_latency_is_reported_per_hook_basename(loaded):
    out = {r["hook"]: r for r in efficiency("04_hook_latency_by_hook.sql",
                                            {"since": SINCE, "until": UNTIL})}
    assert set(out) == {"stdtel-hook", "format-sh"}, out
    assert out["stdtel-hook"]["n_firings"] == 3
    assert out["stdtel-hook"]["n_turns"] == 2, "two Stops in one turn are one turn"
    assert out["stdtel-hook"]["total_ms"] == 12 + 20 + 15
    assert out["format-sh"]["max_ms"] == 300
    assert all("/" not in hook for hook in out), "a hook is a basename, never a path"


def test_cache_creation_share_of_a_skill_tail(loaded):
    out = {r["skill_name"]: r for r in efficiency(
        "05_skill_cache_creation_share.sql", {"since": SINCE, "until": UNTIL})}
    assert set(out) == {"structured-logging", "stdtel-onboard"}, out
    row = out["structured-logging"]
    assert row["n_invocations"] == 1 and row["n_without_usage"] == 0
    tail = 1200 + 800 + 90000 + 4000
    assert row["tail_tokens"] == tail
    assert float(row["cache_creation_share"]) == pytest.approx(4000 / tail, abs=5e-5)


@pytest.mark.parametrize("name", sorted(
    p.name for p in (ROOT / "warehouse" / "efficiency").glob("*.sql")))
def test_every_efficiency_query_declares_what_it_does_not_prove(name):
    """A number without its caveat is the failure mode this project exists for."""
    text = (ROOT / "warehouse" / "efficiency" / name).read_text()
    head = text.split("SELECT")[0]
    assert "ANSWERS:" in head, f"{name}: no statement of the question"
    assert "DOES NOT PROVE:" in head, f"{name}: no statement of the limits"
    assert "version:" in head, f"{name}: unversioned"


def test_every_efficiency_question_is_listed_in_its_readme():
    """A hardcoded count went stale the first time a question was added. What
    matters is that the directory and the index agree: a query nobody can find
    from the README is a query nobody runs."""
    readme = (ROOT / "warehouse" / "efficiency" / "README.md").read_text()
    files = sorted(p.name for p in (ROOT / "warehouse" / "efficiency").glob("*.sql"))
    assert files, "no efficiency queries found"
    missing = [f for f in files if f"`{f}`" not in readme]
    assert not missing, f"not listed in README: {missing}"


def test_no_efficiency_query_counts_turns_with_count_star():
    """A turn emits one row per Stop carrying that slice's delta: two Stops in
    one turn sum correctly and must never be counted twice."""
    import re
    for path in sorted((ROOT / "warehouse" / "efficiency").glob("*.sql")):
        text = path.read_text()
        for match in re.finditer(r"AS\s+(n_turns\w*)", text):
            line = text[:match.start()].rsplit("\n", 1)[-1] + match.group(0)
            assert re.search(r"count\(DISTINCT\s+(?:\w+\.)?prompt_id\)", line), \
                f"{path.name}: {line.strip()}"
