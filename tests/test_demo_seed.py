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
    for pr in data["pull_request"]:
        assert pr["repo"].startswith(DEMO_MARKER.lower()), pr["repo"]
        assert DEMO_MARKER in pr["pr_id"] or pr["repo"].startswith(DEMO_MARKER.lower())
    # "unattributed" is the system's own sentinel for a branch with no ticket key,
    # not an identifier that could be mistaken for real. The demo includes it on
    # purpose, because real data does.
    for inv in data["skill_invocation"]:
        assert inv["ticket_id"].startswith(DEMO_MARKER) or inv["ticket_id"] == "unattributed"


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
    first = {r["pr_id"] for r in data["policy_result"] if r["run_seq"] == 1}
    merged = {p["pr_id"] for p in data["pull_request"] if p["merged_at"]}
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


def test_includes_unattributed_and_unversioned_rows(data):
    """Both are real data-quality faults; a demo without them hides the common case."""
    assert any(i["ticket_id"] == "unattributed" or i["skill_version"] == "unversioned"
               for i in data["skill_invocation"]) or \
           any(s["ticket_id"] == "unattributed" for s in data["session_cost"])


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
