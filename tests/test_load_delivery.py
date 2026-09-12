"""Delivery-side loader: GitHub -> ticket / pull_request / policy_result / defect.

This is the half that turns "what a skill cost" into "what happened to the work
it was used on". Fixtures are synthetic gh JSON so nothing here touches network.
"""
import datetime as dt
import json
from pathlib import Path

import pytest

from warehouse.load_delivery import (TABLES, assisted_by, defect_rows, first_approval_at,
                                     parse_pr, policy_rows, review_rounds, ticket_rows)


def pr(**over):
    base = {
        "number": 7, "headRefName": "feature/PLAT-42-audit-log",
        "createdAt": "2026-09-01T09:00:00Z", "mergedAt": "2026-09-03T09:00:00Z",
        "labels": [{"name": "claude-code"}],
        "reviews": [{"state": "CHANGES_REQUESTED", "submittedAt": "2026-09-01T12:00:00Z"},
                    {"state": "APPROVED", "submittedAt": "2026-09-02T09:00:00Z"}],
    }
    base.update(over)
    return base


# --- the join key ---

def test_ticket_id_comes_from_the_branch():
    assert parse_pr(pr(), "o/r")["ticket_id"] == "PLAT-42"


def test_branch_without_a_ticket_is_unattributed_not_dropped():
    assert parse_pr(pr(headRefName="fix-typo"), "o/r")["ticket_id"] == "unattributed"


def test_unattributed_prs_produce_no_ticket_row():
    """Kept for cost analysis, excluded from outcome analysis — the design rule."""
    rows = ticket_rows([parse_pr(pr(headRefName="fix-typo"), "o/r")], "payments")
    assert rows == []


# --- review rounds and approval latency ---

def test_review_rounds_counts_changes_requested_not_comments():
    assert review_rounds([{"state": "COMMENTED"}, {"state": "COMMENTED"}]) == 0
    assert review_rounds([{"state": "CHANGES_REQUESTED"}, {"state": "APPROVED"}]) == 1


def test_hours_to_first_approval_uses_the_earliest_approval():
    reviews = [{"state": "APPROVED", "submittedAt": "2026-09-02T09:00:00Z"},
               {"state": "APPROVED", "submittedAt": "2026-09-01T10:00:00Z"}]
    assert first_approval_at(reviews).hour == 10
    assert parse_pr(pr(reviews=reviews), "o/r")["hours_to_first_approval"] == 1.0


def test_never_approved_pr_has_no_approval_latency():
    assert parse_pr(pr(reviews=[]), "o/r")["hours_to_first_approval"] is None


# --- crossover arm assignment ---

@pytest.mark.parametrize("labels,expected", [
    ([{"name": "claude-code"}], "claude-code"),
    ([{"name": "copilot"}], "copilot"),
    ([{"name": "no-ai"}], "none"),
    ([{"name": "chore"}], "unknown"),
    ([], "unknown"),
])
def test_assisted_by_reads_the_harness_label(labels, expected):
    assert assisted_by(labels) == expected


def test_unlabelled_is_unknown_never_silently_a_control():
    """Treating missing data as a no-AI control would bias the whole comparison."""
    assert assisted_by([]) != "none"


def test_ticket_with_conflicting_arms_is_marked_mixed():
    prs = [parse_pr(pr(number=1), "o/r"),
           parse_pr(pr(number=2, labels=[{"name": "copilot"}]), "o/r")]
    assert ticket_rows(prs, "t")[0]["harness_arm"] == "mixed"


# --- cycle time ---

def test_cycle_time_spans_first_open_to_last_merge():
    prs = [parse_pr(pr(number=1, createdAt="2026-09-01T00:00:00Z",
                       mergedAt="2026-09-01T12:00:00Z"), "o/r"),
           parse_pr(pr(number=2, createdAt="2026-09-01T06:00:00Z",
                       mergedAt="2026-09-02T00:00:00Z"), "o/r")]
    assert ticket_rows(prs, "t")[0]["cycle_time_hours"] == 24.0


def test_unmerged_work_has_no_cycle_time():
    rows = ticket_rows([parse_pr(pr(mergedAt=None), "o/r")], "t")
    assert rows[0]["cycle_time_hours"] is None and rows[0]["done_at"] is None


# --- policy results carry run_seq, which is the whole metric ---

def test_policy_rows_preserve_run_seq(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(json.dumps({"pr_id": "o/r#7", "policy_id": "logging.no_pii", "run_seq": 1,
                             "passed": False, "evaluated_at": "2026-09-01T09:00:00Z"}) + "\n"
                 + json.dumps({"pr_id": "o/r#7", "policy_id": "logging.no_pii", "run_seq": 2,
                               "passed": True, "evaluated_at": "2026-09-01T10:00:00Z"}) + "\n")
    rows = policy_rows(f)
    assert [r["run_seq"] for r in rows] == [1, 2]
    assert rows[0]["passed"] is False, "first-time pass is what the scorecard measures"
    assert isinstance(rows[0]["evaluated_at"], dt.datetime)


# --- defects link back to the ticket ---

def test_defect_links_to_a_ticket_mentioned_in_the_issue():
    rows = defect_rows([{"number": 3, "title": "NPE after PLAT-42 shipped", "body": "",
                         "createdAt": "2026-09-10T00:00:00Z",
                         "labels": [{"name": "sev2"}]}], "o/r")
    assert rows[0]["ticket_id"] == "PLAT-42" and rows[0]["severity"] == "sev2"


def test_defect_without_a_ticket_reference_is_kept_unlinked():
    rows = defect_rows([{"number": 4, "title": "flaky test", "body": "",
                         "createdAt": "2026-09-10T00:00:00Z", "labels": []}], "o/r")
    assert rows[0]["ticket_id"] is None and rows[0]["defect_id"] == "o/r#4"


# --- schema agreement ---

def test_loader_columns_exist_in_the_schema():
    from tests.test_warehouse import schema_columns
    for table, cols in TABLES.items():
        missing = set(cols) - set(schema_columns(table))
        assert not missing, f"{table}: {sorted(missing)}"


def test_parse_pr_emits_exactly_the_pull_request_columns():
    assert set(parse_pr(pr(), "o/r")) == set(TABLES["pull_request"])
