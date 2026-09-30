"""The synthetic dataset.

Two jobs. It shows a newcomer what a populated scorecard looks like, and it is
the first thing ever to exercise `warehouse/scorecard.sql`, which until now has
only ever returned zero rows.

The overriding constraint: synthetic data must be **unmistakable**. This project
exists to stop numbers looking like measurements when they are not, and a seeded
warehouse that reads as real is precisely that trap.
"""
import os
from pathlib import Path

import pytest

from warehouse.demo_seed import DEMO_MARKER, TABLES, columns, fill, generate

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def data():
    return generate(seed=42)


# --- unmistakably synthetic ---

def test_every_identifier_is_marked_as_demo(data):
    """Nothing here may read as a real ticket, repo or team."""
    for ticket in data["ticket"]:
        assert ticket["ticket_id"].startswith(DEMO_MARKER), ticket["ticket_id"]
    for cr in data["change_request"]:
        assert "/demo-org/" in cr["repo_id"] and "demo-org" in cr["cr_id"], cr["cr_id"]
    for link in data["change_request_ticket"]:
        assert link["ticket_id"].startswith(DEMO_MARKER), link["ticket_id"]
    for ev in data["commit_evidence"]:
        assert ev["patch_id"].startswith("demo-patch-"), ev["patch_id"]


def test_teams_are_obviously_fictional(data):
    teams = {t["team"] for t in data["ticket"]}
    assert teams and all(t.startswith(DEMO_MARKER.lower()) for t in teams), teams


# --- deterministic, so docs and screenshots stay true ---

def test_same_seed_gives_identical_data():
    assert generate(seed=7) == generate(seed=7)


def test_different_seeds_differ():
    assert generate(seed=7) != generate(seed=8)


# --- shaped for the scorecard ---

def test_covers_every_table_the_scorecard_joins(data):
    assert set(data) == set(TABLES)
    for table, rows in data.items():
        assert rows, f"{table} is empty; the scorecard would return nothing"


def test_rows_match_the_schema(data):
    from tests.test_warehouse import schema_columns
    for table, rows in data.items():
        expected = set(schema_columns(table))
        for row in rows:
            assert set(row) <= expected, f"{table}: unknown columns {set(row) - expected}"


def test_has_both_arms_so_a_comparison_is_possible(data):
    arms = {t["harness_arm"] for t in data["ticket"]}
    assert {"claude-code", "none"} <= arms, arms


def test_first_ci_run_is_recorded_for_every_pr(data):
    """run_seq=1 is what first-time pass rate reads."""
    first = {r["cr_id"] for r in data["policy_result"] if r["run_seq"] == 1}
    merged = {c["cr_id"] for c in data["change_request"] if c["state"] == "merged"}
    assert merged <= first, "a merged PR with no first CI run cannot be scored"


def test_some_prs_needed_more_than_one_run(data):
    """If every PR passed first time there is no signal to measure."""
    assert any(r["run_seq"] > 1 for r in data["policy_result"])


# --- the picture it paints must teach, not flatter ---

def test_the_demo_does_not_only_show_success(data):
    """A dataset where every skill wins teaches nothing and sets a false expectation."""
    passed = [r["passed"] for r in data["policy_result"] if r["run_seq"] == 1]
    rate = sum(passed) / len(passed)
    assert 0.3 < rate < 0.95, f"first-time pass rate {rate:.2f} is not a realistic mix"


def test_includes_a_skill_with_too_little_data_to_judge(data):
    """The correct answer is often 'not enough data'; the demo should show one."""
    from collections import Counter
    counts = Counter(i["skill_name"] for i in data["skill_invocation"])
    assert min(counts.values()) < 10, f"no under-sampled skill to demonstrate refusal: {counts}"


def test_includes_the_faults_that_still_bound_a_conclusion(data):
    """ADR-013 moved the fault: any branch joins, so what bounds a conclusion is
    activity with no branch identity, a skill link with no commit evidence, and
    unversioned skills. A demo without them hides the common cases."""
    assert any(s["branch_hash"] is None for s in data["session_cost"]), "no identity-less session"
    assert any(i["skill_version"] == "unversioned" for i in data["skill_invocation"])
    confirmed = {c["cr_id"] for c in data["change_request_commit"]}
    used = {c["cr_id"] for c in data["change_request"]
            if any(i["branch_hash"] == c["branch_hash"] for i in data["skill_invocation"])}
    assert used - confirmed, "no skill-using change request without evidence: the uncertain arm is never shown"
    assert any(c["state"] == "closed" for c in data["change_request"]), "no abandoned change request"
    assert any(c["source_branch"].startswith("fix/demo-hardening-") for c in data["change_request"]), \
        "no branch without a ticket key: the case ADR-013 exists for"


# --- the seeder must write every column, not just the first row's ------------

def test_columns_are_the_union_across_rows_not_the_first_rows_keys():
    """Kinds do not share a shape: a turn carries no `external_system` and an
    external run carries no `hook_ms`. Taking the first row's keys meant every
    column the first kind happened to lack was dropped for every later row —
    silently, because psycopg ignores extra keys in a parameter dict."""
    assert columns([{"a": 1, "b": 2}, {"a": 3, "c": 4}]) == ["a", "b", "c"]


def test_a_row_missing_a_column_is_filled_with_null_not_skipped():
    """Once the list is a union, every row must supply every key or the insert
    raises. NULL is the truthful filler: a turn has no external cost, and that is
    an absence rather than a zero (ADR-005)."""
    rows = [{"a": 1}, {"b": 2}]
    assert fill(rows, columns(rows)) == [{"a": 1, "b": None}, {"a": None, "b": 2}]


def test_every_seeded_artefact_kind_carries_its_own_columns(data):
    """The regression this is about: external rows reached the warehouse with a
    NULL system, operation and cost, so the external query returned one empty row
    and read as a broken query rather than a broken seeder."""
    rows = data["artefact_activation"]
    assert "external" in {r["kind"] for r in rows}, "the fleet seeds no external spend to show"
    ext = [r for r in rows if r["kind"] == "external"]
    assert all(r["external_system"] for r in ext)
    assert any(r["external_cost_usd"] is not None for r in ext), "no cost to total"
    assert any(r["external_cost_usd"] is None for r in ext), \
        "every run reporting a cost hides what thin coverage looks like"
    cols = columns(rows)
    for c in ("external_system", "external_cost_usd", "scope_name", "hook_ms_by_hook"):
        assert c in cols, f"{c} would never reach the database"


def test_the_demo_fleet_shows_every_provenance_query_seven_separates():
    """Query 7 version 2 splits external spend five ways. A fleet that seeds only
    billed-and-labelled runs shows four columns of zero, which reads as a broken
    query rather than an empty category (#88)."""
    ext = [r for r in generate(seed=42)["artefact_activation"] if r["kind"] == "external"]
    billed = [r for r in ext if r["external_cost_usd"] is not None]
    assert any(r["external_cost_source"] == "provider" for r in billed)
    assert any(r["external_cost_source"] is None for r in billed), "no unlabelled (pre-v2) runs"
    assert any(r["external_cost_estimated_usd"] is not None for r in billed), "no mixed runs"
    assert any(r["external_cost_usd"] is None and r["external_cost_estimated_usd"] is not None
               for r in ext), "no estimate-only runs"
    assert any(r["external_cost_usd"] is None and r["external_cost_estimated_usd"] is None
               for r in ext), "no run with nothing observed"
    assert any(r["session_id"] == "" for r in ext), "no run from outside a Claude session"
    assert all(r["external_cost_source"] in (None, "provider", "local") for r in ext)


def test_the_demo_fleet_shows_the_v3_join_and_partial_runs():
    """Query 7 version 3 adds a join rate and a lower-bound column. A fleet with
    no mcp_tool_call rows reads 0 joined, which is indistinguishable from the
    undocumented `_meta` key having vanished (ADR-012)."""
    data = generate(seed=42)
    acts = {r["span_id"]: r for r in data["artefact_activation"]}
    calls = {c["tool_use_id"]: c for c in data["mcp_tool_call"]}
    ext = [r for r in acts.values() if r["kind"] == "external"]
    with_id = [r for r in ext if r.get("external_tool_use_id")]
    joined = [r for r in with_id if r["external_tool_use_id"] in calls]
    assert joined, "no external run resolves to a call"
    assert len(joined) < len(with_id), "a join rate of exactly 100% hides what the column is for"
    for r in joined:
        c = calls[r["external_tool_use_id"]]
        assert acts[c["activation_span_id"]]["kind"] == "turn", "a call maps to a real demo turn"
    assert not any(r["session_id"] == "" and r.get("external_tool_use_id") for r in ext), \
        "a run outside Claude Code has no tool call to carry"
    assert any((r.get("external_requests_unpriced") or 0) > 0 and r["external_cost_usd"] is not None
               for r in ext), "no partial run"
    assert all(c["tool_use_id"].startswith("demo-") for c in calls.values()), "clear() matches demo- only"
