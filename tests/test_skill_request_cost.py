"""ADR-014 decisions 1 and 10, #117: a skill's cost is the sum of the harness's own
requests named for it, not stdtel's tail-rule estimate.

`skill_request_cost` is one row per (session, prompt, skill): a skill invoked
twice in one prompt must not claim the same requests twice (#23). Its version
comes from stdtel's activation, because the harness does not know it. Where no
request names the skill there is no row, and whether that means "abandoned" or
"not measured" depends on whether the prompt has any native record at all —
that is `skill_activation_cost`, which keeps the two apart (ADR-005).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
T0 = dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc)
S = "wtest-src-sess"


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
    cur.execute("DELETE FROM llm_request WHERE request_id LIKE 'wtest-src-%%'")
    cur.execute("DELETE FROM artefact_activation WHERE span_id LIKE 'wtest-src-%%'")
    cur.execute("DELETE FROM skill_invocation WHERE span_id LIKE 'wtest-src-%%'")


def activation(cur, span, prompt, name, version):
    cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, ended_at, kind, "
                "name, prompt_id, source, harness) VALUES (%s,'t',%s,%s,%s,'skill',%s,%s,'hook','claude-code')",
                (span, S, T0, T0, name, prompt))
    cur.execute("INSERT INTO skill_invocation (span_id, trace_id, session_id, started_at, ended_at, harness, "
                "skill_name, skill_version) VALUES (%s,'t',%s,%s,%s,'claude-code',%s,%s)",
                (span, S, T0, T0, name, version))


def request(cur, rid, prompt, skill, cost, inp=10, out=5, cr=100, cc=20):
    cur.execute("INSERT INTO llm_request (harness, request_id, session_id, prompt_id, ended_at, skill_name, "
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, "
                "attribution_source) VALUES ('claude-code',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'native')",
                (rid, S, prompt, T0, skill, inp, out, cr, cc, cost))


@pytest.fixture(scope="module")
def db():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        _clean(cur)
        # p1: lint invoked twice in one prompt; three requests name it, one names nothing
        activation(cur, "wtest-src-a1", "p1", "acme-lint", "1.2.0")
        activation(cur, "wtest-src-a2", "p1", "acme-lint", "1.2.0")
        for i, cost in enumerate(("0.01", "0.02", "0.0000003")):
            request(cur, f"wtest-src-r1{i}", "p1", "acme-lint", cost)
        request(cur, "wtest-src-r19", "p1", None, "0.5")
        # p2: lint ran, the prompt has native records, none name it -> abandoned
        activation(cur, "wtest-src-a3", "p2", "acme-lint", "1.2.0")
        request(cur, "wtest-src-r20", "p2", None, "0.1")
        # p3: lint ran, no native record for the prompt at all -> not measured
        activation(cur, "wtest-src-a4", "p3", "acme-lint", "1.2.0")
        # p4: requests name a skill stdtel never saw -> cost with no version
        request(cur, "wtest-src-r40", "p4", "ghost-skill", "0.3")
        # p5: a namespaced native name matches the bare catalogue name
        activation(cur, "wtest-src-a5", "p5", "format", "2.0.0")
        request(cur, "wtest-src-r50", "p5", "acme:format", "0.04")
        # p6: two versions of one skill in one prompt -> version unknown, cost counted once
        activation(cur, "wtest-src-a6", "p6", "acme-lint", "1.2.0")
        activation(cur, "wtest-src-a7", "p6", "acme-lint", "1.3.0")
        request(cur, "wtest-src-r60", "p6", "acme-lint", "0.06")
        # p7: still "third-party" (nothing to name it from): not a skill called third-party
        request(cur, "wtest-src-r70", "p7", "third-party", "0.07")
        # p8, p9: one scoped run of a loop skill over two turns (ADR-010). In p8 the
        # loop itself makes one request and a sub-agent makes another; p9 is a later
        # iteration where the loop does not activate again; p10 is in scope but has
        # no native record.
        for prompt, loop in (("p8", True), ("p9", False), ("p10", False)):
            scope = ("wtest-loop", "k1", "wtest-scope-1", "artefact")
            cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, ended_at, "
                        "kind, prompt_id, source, harness, scope_name, scope_key, scope_id, scope_source) "
                        "VALUES (%s,'t',%s,%s,%s,'turn',%s,'hook','claude-code',%s,%s,%s,%s)",
                        (f"wtest-src-turn-{prompt}", S, T0, T0, prompt, *scope))
            if loop:
                activation(cur, f"wtest-src-loop-{prompt}", prompt, "wtest-loop", "0.1.0")
                cur.execute("UPDATE artefact_activation SET scope_name=%s, scope_key=%s, scope_id=%s, "
                            "scope_source=%s WHERE span_id = %s", (*scope, f"wtest-src-loop-{prompt}"))
        # spend the harness cannot see, reported by an external emitter inside the scope
        cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, ended_at, kind, "
                    "name, prompt_id, source, harness, scope_name, scope_key, scope_id, scope_source, "
                    "input_tokens, output_tokens) VALUES ('wtest-src-ext','t',%s,%s,%s,'external','llm-council',"
                    "'p8','hook','claude-code','wtest-loop','k1','wtest-scope-1','artefact',7000,700)", (S, T0, T0))
        # p11: a second prompt where lint ran with no native record
        activation(cur, "wtest-src-a11", "p11", "acme-lint", "1.2.0")
        request(cur, "wtest-src-r80", "p8", "wtest-loop", "0.10", inp=100, out=10, cr=0, cc=0)
        request(cur, "wtest-src-r81", "p8", None, "0.20", inp=400, out=40, cr=0, cc=0)
        request(cur, "wtest-src-r90", "p9", None, "0.30", inp=1000, out=100, cr=0, cc=0)
    yield conn
    with conn.cursor() as cur:
        _clean(cur)
    conn.close()


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def cost_rows(conn):
    return {(r["prompt_id"], r["skill_name"]): r for r in
            q(conn, "SELECT * FROM skill_request_cost WHERE session_id = %s", (S,))}


def test_one_row_per_prompt_and_skill_however_often_it_ran(db):
    r = cost_rows(db)[("p1", "acme-lint")]
    assert r["requests"] == 3 and r["skill_version"] == "1.2.0"
    assert r["cost_usd"] == Decimal("0.0300003")
    assert (r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["cache_creation_tokens"]) == \
        (30, 15, 300, 60)


def test_the_view_never_counts_a_request_twice(db):
    """The sum over the view equals the sum of the requests it covers."""
    total = q(db, "SELECT sum(cost_usd) AS s FROM skill_request_cost WHERE session_id = %s", (S,))[0]["s"]
    direct = q(db, "SELECT sum(cost_usd) AS s FROM llm_request_attributed WHERE session_id = %s "
                   "AND skill_name IS NOT NULL AND skill_name <> 'third-party'", (S,))[0]["s"]
    assert total == direct


def test_a_skill_stdtel_never_saw_still_has_its_cost_with_no_version(db):
    r = cost_rows(db)[("p4", "ghost-skill")]
    assert r["cost_usd"] == Decimal("0.3") and r["skill_version"] is None


def test_a_namespaced_native_name_finds_the_bare_catalogue_version(db):
    assert cost_rows(db)[("p5", "acme:format")]["skill_version"] == "2.0.0"


def test_two_versions_in_one_prompt_are_unknown_not_doubled(db):
    rows = [r for r in cost_rows(db).values() if r["prompt_id"] == "p6"]
    assert len(rows) == 1 and rows[0]["skill_version"] is None and rows[0]["cost_usd"] == Decimal("0.06")


# --- per activation: abandoned, measured, or not measured ---------------------------------------

def act_rows(db):
    return {r["prompt_id"]: r for r in
            q(db, "SELECT * FROM skill_activation_cost WHERE session_id = %s", (S,))}


def test_an_activation_with_requests_is_not_abandoned(db):
    r = act_rows(db)["p1"]
    assert r["abandoned"] is False and r["requests"] == 3 and r["skill_version"] == "1.2.0"


def test_no_request_named_while_the_prompt_was_measured_is_abandoned(db):
    r = act_rows(db)["p2"]
    assert r["abandoned"] is True and r["requests"] == 0 and r["cost_usd"] is None


def test_no_native_record_at_all_is_not_measured_never_abandoned(db):
    """A session without native telemetry is not a session of abandoned skills."""
    r = act_rows(db)["p3"]
    assert r["abandoned"] is None and r["requests"] is None


def test_one_activation_row_per_prompt_skill_and_version(db):
    rows = q(db, "SELECT prompt_id FROM skill_activation_cost WHERE session_id = %s", (S,))
    assert sorted(r["prompt_id"] for r in rows) == ["p1", "p11", "p2", "p3", "p5", "p6", "p6", "p8"]


def test_an_ambiguous_version_is_not_abandoned_and_its_cost_goes_to_neither(db):
    rows = [r for r in q(db, "SELECT * FROM skill_activation_cost WHERE session_id = %s AND prompt_id = 'p6'", (S,))]
    assert {r["skill_version"] for r in rows} == {"1.2.0", "1.3.0"}
    assert all(r["abandoned"] is False and r["cost_usd"] is None for r in rows)


def test_an_unnamed_third_party_request_is_not_a_skill_called_third_party(db):
    assert not [k for k in cost_rows(db) if k[1] == "third-party"]


# --- the efficiency queries that read skill cost, rebased on native requests --------------------

def efficiency(conn, name, params):
    sql = (ROOT / "warehouse" / "efficiency" / name).read_text()
    for key in params:
        sql = sql.replace(f":{key}", f"%({key})s")
    return q(conn, sql, params)


WINDOW = {"since": T0 - dt.timedelta(days=1), "until": T0 + dt.timedelta(days=1)}


def test_q5_reads_cache_creation_from_the_harness_record(db):
    out = {r["skill_name"]: r for r in efficiency(db, "05_skill_cache_creation_share.sql", WINDOW)}
    lint = out["acme-lint"]
    # p1 (three requests) and p6 (one), each 10+5+100+20 tokens, 20 of them cache creation
    assert lint["skill_tokens"] == 4 * 135 and lint["cache_creation_tokens"] == 4 * 20
    assert float(lint["cache_creation_share"]) == pytest.approx(20 / 135, abs=5e-5)
    assert lint["n_prompts"] == 2


def test_q5_counts_activations_with_no_native_record_rather_than_hiding_them(db):
    out = {r["skill_name"]: r for r in efficiency(db, "05_skill_cache_creation_share.sql", WINDOW)}
    assert out["acme-lint"]["n_unmeasured"] == 2          # p3 and p11; p2 was measured and abandoned


def test_q6_self_and_inclusive_count_each_request_once(db):
    rows = efficiency(db, "06_scope_self_vs_inclusive.sql", {"scope_name": "wtest-loop", "since": WINDOW["since"]})
    assert len(rows) == 1
    r = rows[0]
    assert r["self_tokens"] == 110                          # the loop's own request
    assert r["inclusive_tokens"] == 110 + 440 + 1100 + 7700  # every request in a scoped prompt, and external spend
    assert r["n_turns"] == 3 and r["n_runs"] == 1
    assert r["prompts_without_usage"] == 1                  # p10


def test_q6_inclusive_is_never_less_than_self(db):
    for r in efficiency(db, "06_scope_self_vs_inclusive.sql", {"scope_name": "wtest-loop", "since": WINDOW["since"]}):
        assert r["inclusive_tokens"] >= r["self_tokens"]


def test_the_scorecard_charges_each_version_only_its_own_cost(db):
    """Two versions of one skill: cost must not be joined across versions."""
    sql = (ROOT / "warehouse" / "scorecard.sql").read_text()
    sql = sql.replace(":week_start", "%(a)s").replace(":week_end", "%(b)s")
    rows = {(r["skill_name"], r["skill_version"]): r for r in
            q(db, sql, {"a": T0 - dt.timedelta(days=1), "b": T0 + dt.timedelta(days=1)})}
    assert rows[("acme-lint", "1.2.0")]["total_cost_usd"] == Decimal("0.0300003")   # p1 only; p6 is ambiguous
    assert rows[("acme-lint", "1.3.0")]["total_cost_usd"] is None


def test_q1s_skill_row_is_the_harness_record_for_the_session(db):
    """A skill activation carries no usage since #117; left alone, Q1 showed every
    skill row as 100% without usage, which reads as a broken loader."""
    out = {r["kind"]: r for r in efficiency(db, "01_tokens_by_artefact_kind.sql", {"session_id": S})}
    skill = out["skill"]
    direct = q(db, "SELECT sum(input_tokens) AS i, sum(output_tokens) AS o, sum(requests) AS n "
                   "FROM skill_request_cost WHERE session_id = %s", (S,))[0]
    assert (skill["input_tokens"], skill["output_tokens"], skill["llm_requests"]) == \
        (direct["i"], direct["o"], direct["n"])
    # activations in a prompt with no native record at all: p3 and p11
    assert skill["n_without_usage"] == 2
