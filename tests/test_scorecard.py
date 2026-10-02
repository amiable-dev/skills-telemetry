"""The scorecard query itself.

`scorecard.sql` computes the primary metric and had no test at all until the
synthetic dataset made it runnable. Its first execution reported n_with = 3192
against 120 merged PRs (#23), which is the kind of defect that only shows when a
query meets rows.

These are integration tests: they need Postgres, and skip without it.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
PG = "deploy-postgres-1"


def _connect():
    """Straight to the published port. Going through `docker exec` made these
    tests depend on the CLI resolving its context, which it does not do under
    pytest."""
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed (pip install -e '.[warehouse]')")
    try:
        return psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:                        # noqa: BLE001 - environment, not logic
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")


def psql(sql: str):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None


def scorecard_rows() -> list[dict]:
    """Run scorecard.sql with its parameters bound."""
    sql = (ROOT / "warehouse" / "scorecard.sql").read_text()
    sql = sql.replace(":week_start", "%(week_start)s").replace(":week_end", "%(week_end)s")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, {"week_start": "2026-07-01", "week_end": "2026-09-01"})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@pytest.fixture(scope="module", autouse=True)
def seeded():
    """Load the synthetic fleet; leave the warehouse as we found it."""
    # Apply the schema first. It is idempotent, and without it these tests read
    # whatever columns an *earlier* test module happened to add: a query using a
    # new column failed in a full run and passed alone, depending only on the
    # alphabetical order of test files (#88).
    with _connect() as conn, conn.cursor() as cur:
        cur.execute((Path(__file__).resolve().parent.parent / "warehouse" / "schema.sql").read_text())
    from warehouse.demo_seed import clear, generate, load
    try:
        load(DSN, generate(seed=42))
    except Exception as e:                       # noqa: BLE001 - environment, not logic
        pytest.skip(f"cannot seed: {e}")
    yield
    clear(DSN)


def test_the_scorecard_returns_rows(seeded):
    assert scorecard_rows(), "with the demo data loaded the scorecard must produce rows"


def test_n_with_cannot_exceed_the_merged_prs_in_the_window(seeded):
    """The defect that started #23: a cartesian join reported 3192 against 120 PRs.

    n counts PRs, so it is bounded by the PRs that exist. Every sample-size rule
    in this project assumes n is truthful.
    """
    merged = int(psql("SELECT count(*) FROM change_request WHERE state = 'merged'"))
    for row in scorecard_rows():
        for column in ("n_with", "n_without"):
            if row[column] is not None:
                assert int(row[column]) <= merged, (
                    f"{row['skill_name']}: {column}={row[column]} exceeds {merged} merged PRs")


def test_a_skill_with_no_outcome_data_is_not_recommended_keep(seeded):
    """`uncatalogued-helper` had NULL rates in both arms and was recommended keep."""
    for row in scorecard_rows():
        if row["first_pass_rate_with"] is None and row["first_pass_rate_without"] is None:
            assert row["recommended_action"] != "keep", row
            assert "insufficient" in row["recommended_action"], row


def test_below_the_floor_is_insufficient_data_even_with_rates(seeded):
    """30 merged PRs per arm is the floor from evaluation-power.md."""
    for row in scorecard_rows():
        n_with = int(row["n_with"]) if row["n_with"] is not None else 0
        if 0 < n_with < 30:
            assert "insufficient" in row["recommended_action"], row


def test_a_pr_counts_once_however_often_the_skill_was_invoked(seeded):
    """The metric is first-time pass rate *on the PR*, so the unit is the PR."""
    rows = {r["skill_name"]: r for r in scorecard_rows()}
    row = rows.get("structured-logging")
    assert row, "the demo data should exercise this skill"
    prs_using = int(psql(
        "SELECT count(DISTINCT v.cr_id) FROM activation_change_request v "
        "JOIN skill_invocation i ON i.span_id = v.span_id "
        "WHERE i.skill_name = 'structured-logging'"))
    assert int(row["n_with"]) <= prs_using, f"n_with={row['n_with']} > {prs_using} PRs using it"


# --- #43: distinct_users read 0 for every skill, at every volume ---

def test_the_demo_fleet_has_more_than_one_developer(seeded):
    """A synthetic pack with one user cannot exercise the branch below."""
    assert int(psql("SELECT count(DISTINCT user_hash) FROM skill_invocation")) > 1


def test_review_single_user_fires_when_only_one_person_used_a_skill(seeded):
    """The branch existed and could never be reached.

    `std.user.hash` was derived at the collector from `user.email`, which nothing
    ever set, so COUNT(DISTINCT user_hash) was 0 — never 1 — and a skill used by
    exactly one person was never flagged. Vacuous truth is a bug (ADR-005).
    """
    above_floor = [r["skill_name"] for r in scorecard_rows()
                   if "insufficient" not in r["recommended_action"]]
    if not above_floor:
        pytest.skip("no demo skill clears the 30-PR floor; the branch is unreachable here")
    skill = above_floor[0]
    with _connect() as conn, conn.cursor() as cur:
        # span_id LIKE 'demo%' is the fence. Demo skill names collide with real
        # ones — `structured-logging` is both — and an unscoped UPDATE here
        # rewrote user_hash on eight real rows that `clear()` then had no reason
        # to remove. A test that corrupts the warehouse it is checking is worse
        # than no test; the teardown deletes demo rows, so scoping is enough.
        cur.execute("UPDATE skill_invocation SET user_hash = 'demo-user-solo' "
                    "WHERE skill_name = %s AND span_id LIKE 'demo%%'", (skill,))
        assert cur.rowcount, "no demo rows matched: the fence would make this test vacuous"
        conn.commit()
    rows = {r["skill_name"]: r for r in scorecard_rows()}
    assert int(rows[skill]["distinct_users"]) == 1
    assert rows[skill]["recommended_action"] == "review-single-user", rows[skill]


# --- ADR-009: the demo fleet must populate every efficiency query -------------

def _efficiency_files():
    return sorted((ROOT / "warehouse" / "efficiency").glob("[0-9]*.sql"))


def _run_efficiency(path, **params):
    """Run one efficiency query with its `:name` parameters bound."""
    sql = path.read_text()
    bound = dict(params)
    for name in bound:
        sql = sql.replace(f":{name}", f"%({name})s")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, bound)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_there_are_efficiency_queries_to_run():
    assert _efficiency_files(), "no efficiency queries found to exercise"


@pytest.mark.parametrize("path", _efficiency_files(), ids=lambda p: p.stem)
def test_the_demo_fleet_answers_every_efficiency_question(path, seeded):
    """`make demo` exists so a newcomer sees a populated dashboard rather than a
    blank one they cannot tell from a broken install.

    Caught a real gap: the seeder wrote `skill_invocation` rows but no
    `kind=skill` activations, so the skill-side query returned zero rows while
    the scorecard returned plenty — which reads as a broken query rather than a
    half-seeded fleet.
    """
    session_id = psql("SELECT session_id FROM artefact_activation "
                      "WHERE kind = 'turn' AND span_id LIKE 'demoact-%' LIMIT 1")
    assert session_id, "the demo fleet seeded no turn activations"
    rows = _run_efficiency(path, session_id=session_id, scope_name="demo-epic-loop",
                           since="2026-06-01", until="2026-10-01")
    assert rows, f"{path.name} returns nothing against the demo fleet"


def test_turns_are_counted_distinctly_not_per_row(seeded):
    """A turn emits one activation per Stop carrying that slice's delta, so two
    Stops inside one turn are two rows. `count(*)` would report twice the turns
    that happened."""
    rows = _run_efficiency(ROOT / "warehouse" / "efficiency" / "01_tokens_by_artefact_kind.sql",
                           session_id=psql("SELECT session_id FROM artefact_activation "
                                           "WHERE kind='turn' AND span_id LIKE 'demoact-%' LIMIT 1"))
    turn_row = next(r for r in rows if r["kind"] == "turn")
    assert turn_row["n_turns"] <= turn_row["n_activations"]



# --- ADR-013: the join, and who it keeps out of the comparison ------------------------

def test_every_attribution_says_how_it_was_made(seeded):
    methods = set((psql("SELECT string_agg(DISTINCT method, ' ') FROM activation_change_request") or "").split())
    assert methods <= {"branch", "branch+commit", "commit"}, methods
    assert "branch+commit" in methods, "the demo fleet confirms most links with commit evidence"
    assert "branch" in methods, "and leaves some unconfirmed, which the arms must exclude"


def test_a_branch_only_link_is_in_neither_arm(seeded):
    """ADR-002's "unknown, never none", applied to the weaker join: a change
    request linked to a skill by branch alone must not be counted as a change
    request that did not use it."""
    leaked = int(psql("""
        SELECT count(*) FROM (
          SELECT DISTINCT v.cr_id FROM activation_change_request v
          JOIN skill_invocation i ON i.span_id = v.span_id AND i.skill_name = 'structured-logging'
          WHERE v.method = 'branch'
            AND v.cr_id NOT IN (SELECT v2.cr_id FROM activation_change_request v2
                                JOIN skill_invocation i2 ON i2.span_id = v2.span_id
                                 AND i2.skill_name = 'structured-logging'
                                WHERE v2.method <> 'branch')) u"""))
    assert leaked > 0, "the demo must exercise the uncertain case"
    rows = {r["skill_name"]: r for r in scorecard_rows()}
    total_merged_with_policy = int(psql(
        "SELECT count(DISTINCT c.cr_id) FROM change_request c JOIN policy_result p ON p.cr_id = c.cr_id "
        "WHERE c.state = 'merged' AND p.policy_id = ANY(ARRAY['logging.required_fields','logging.no_pii'])"))
    r = rows["structured-logging"]
    assert int(r["n_with"]) + int(r["n_without"]) <= total_merged_with_policy - leaked, \
        "a branch-only change request was counted in an arm"



# --- #117: cost comes from the harness's own requests --------------------------------------------

RETIRED = ("p50_load_tokens", "p50_tail_tokens")


def test_the_scorecard_reports_native_cost_not_the_tail_estimate(seeded):
    rows = scorecard_rows()
    assert rows
    for col in ("total_cost_usd", "total_tokens", "p50_tokens", "abandonment_rate", "unmeasured_activations",
                "requests_derived"):
        assert col in rows[0], col
    for col in RETIRED:
        assert col not in rows[0], col
    assert any(r["total_cost_usd"] for r in rows), "the demo fleet must show some cost"


def test_scorecard_cost_reconciles_with_the_activation_view(seeded):
    """The scorecard sums skill_activation_cost; it must not multiply it."""
    rows = scorecard_rows()
    for r in rows:
        if r["total_cost_usd"] is None:
            continue
        direct = psql(f"SELECT sum(cost_usd) FROM skill_activation_cost WHERE skill_name = '{r['skill_name']}' "
                      f"AND skill_version = '{r['skill_version']}' AND harness = '{r['harness']}' "
                      "AND started_at >= '2026-07-01' AND started_at < '2026-09-01'")
        assert r["total_cost_usd"] == direct, r["skill_name"]


def test_scorecard_abandonment_ignores_unmeasured_activations(seeded):
    """A session without native telemetry is not a session of abandoned skills."""
    for r in scorecard_rows():
        got = psql(f"SELECT avg(CASE WHEN abandoned THEN 1 ELSE 0 END) FROM skill_activation_cost "
                   f"WHERE abandoned IS NOT NULL AND skill_name = '{r['skill_name']}' "
                   f"AND skill_version = '{r['skill_version']}' AND harness = '{r['harness']}' "
                   "AND started_at >= '2026-07-01' AND started_at < '2026-09-01'")
        unmeasured = psql(f"SELECT count(*) FROM skill_activation_cost WHERE abandoned IS NULL "
                          f"AND skill_name = '{r['skill_name']}' AND skill_version = '{r['skill_version']}' "
                          f"AND harness = '{r['harness']}' "
                          "AND started_at >= '2026-07-01' AND started_at < '2026-09-01'")
        assert r["abandonment_rate"] == got, r["skill_name"]
        assert (r["unmeasured_activations"] or 0) == unmeasured, r["skill_name"]
    assert any(r["unmeasured_activations"] for r in scorecard_rows()), "the demo must show unmeasured ones"
