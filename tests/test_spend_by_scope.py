"""ADR-016 decision 3 (#159): spend by scope, from the only store that joins them.

A Claude Code request carries its prompt id; a stdtel turn with that prompt id
carries the scope (ADR-010). `llm_request_scope` is one row per request, in one
of four states:
- `scoped`: the turn names one scope;
- `unscoped`: the turn names none;
- `several`: the prompt's turns name more than one scope, so none is guessed (ADR-005);
- `unjoined`: no stdtel turn has the prompt id. This is its own state, never folded
  into "no scope".

Only turns count: a sub-agent shares its parent's prompt id, and a prompt has
several turn deltas (ADR-009), so each request must appear exactly once (#23).
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()
BOARD = json.loads((ROOT / "deploy" / "grafana" / "provisioning" / "dashboards" / "skill-scorecard.json").read_text())
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
T0 = dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc)
S = "wtest-sbs-sess"


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
    cur.execute("DELETE FROM llm_request WHERE request_id LIKE 'wtest-sbs-%%'")
    cur.execute("DELETE FROM artefact_activation WHERE span_id LIKE 'wtest-sbs-%%'")


def turn(cur, span, prompt, scope, kind="turn"):
    cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, ended_at, kind, "
                "name, prompt_id, source, harness, scope_name) "
                "VALUES (%s,'t',%s,%s,%s,%s,%s,%s,'hook','claude-code',%s)",
                (span, S, T0, T0, kind, None if kind == "turn" else "Explore", prompt, scope))


def request(cur, rid, prompt, cost):
    cur.execute("INSERT INTO llm_request (harness, request_id, session_id, prompt_id, ended_at, cost_usd, "
                "attribution_source) VALUES ('claude-code',%s,%s,%s,%s,%s,'native')",
                (rid, S, prompt, T0, cost))


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        _clean(cur)
        # p1: two turn deltas, both scoped epic-loop; two requests
        turn(cur, "wtest-sbs-t1a", "p1", "epic-loop")
        turn(cur, "wtest-sbs-t1b", "p1", "epic-loop")
        request(cur, "wtest-sbs-r1a", "p1", "1.00")
        request(cur, "wtest-sbs-r1b", "p1", "2.00")
        # p2: a turn with no scope
        turn(cur, "wtest-sbs-t2", "p2", None)
        request(cur, "wtest-sbs-r2", "p2", "0.50")
        # p3: turn deltas naming two scopes; one of them also unscoped
        turn(cur, "wtest-sbs-t3a", "p3", "epic-loop")
        turn(cur, "wtest-sbs-t3b", "p3", "deploy")
        turn(cur, "wtest-sbs-t3c", "p3", None)
        request(cur, "wtest-sbs-r3", "p3", "0.25")
        # p4: no stdtel turn at all, only a sub-agent sharing the prompt id
        turn(cur, "wtest-sbs-s4", "p4", "epic-loop", kind="subagent")
        request(cur, "wtest-sbs-r4", "p4", "0.75")
        # p5: one scoped delta and one unscoped delta: the scope is the one named
        turn(cur, "wtest-sbs-t5a", "p5", "epic-loop")
        turn(cur, "wtest-sbs-t5b", "p5", None)
        request(cur, "wtest-sbs-r5", "p5", "0.10")
    yield conn
    with conn.cursor() as cur:
        _clean(cur)
    conn.close()


def rows(db) -> dict[str, tuple]:
    with db.cursor() as cur:
        cur.execute("SELECT request_id, scope_state, scope_name, cost_usd FROM llm_request_scope "
                    "WHERE request_id LIKE 'wtest-sbs-%%'")
        return {r[0]: r[1:] for r in cur.fetchall()}


def test_one_row_per_request_whatever_the_turn_deltas(db):
    assert len(rows(db)) == 6


def test_a_scoped_turn_names_its_scope(db):
    r = rows(db)
    assert r["wtest-sbs-r1a"][:2] == ("scoped", "epic-loop")
    assert r["wtest-sbs-r5"][:2] == ("scoped", "epic-loop")


def test_a_turn_with_no_scope_is_unscoped(db):
    assert rows(db)["wtest-sbs-r2"][:2] == ("unscoped", None)


def test_two_scopes_on_one_prompt_are_not_guessed(db):
    assert rows(db)["wtest-sbs-r3"][:2] == ("several", None)


def test_a_prompt_with_no_turn_is_unjoined_even_with_a_subagent(db):
    assert rows(db)["wtest-sbs-r4"][:2] == ("unjoined", None)


def test_the_view_keeps_every_column_a_panel_needs(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM llm_request_scope LIMIT 0")
        cols = {d.name for d in cur.description}
    assert {"harness", "request_id", "session_id", "prompt_id", "ended_at", "cost_usd",
            "scope_name", "scope_state"} <= cols


# --- the panel's own SQL, run against the seeded rows ----------------------------------------------

def panel_sql() -> str:
    (p,) = [p for p in BOARD["panels"] if p["title"].lower().startswith("spend by scope")]
    return p["targets"][0]["rawSql"]


def test_the_panel_query_runs_and_keeps_unjoined_apart(db):
    sql = (panel_sql().replace("$__timeFrom()", "'1999-01-01T00:00:00Z'")
           .replace("$__timeTo()", "'1999-01-01T00:00:01Z' AND request_id LIKE 'wtest-sbs-%%'"))
    with db.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        got = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    assert set(got) == {"epic-loop", "(no scope)", "(several scopes)", "(prompt joins no stdtel turn)"}
    assert got["epic-loop"]["spend_usd"] == Decimal("3.10")
    assert got["epic-loop"]["requests"] == 3
    assert got["(prompt joins no stdtel turn)"]["spend_usd"] == Decimal("0.75")
    total = sum(r["share"] for r in got.values())
    assert abs(total - 1) < Decimal("0.01"), "shares of the spend in range add up to the whole"
    assert all("data_through" in r for r in got.values()), "every row names the warehouse's age"
