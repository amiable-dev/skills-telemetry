"""CI-side policy grading: the producer of `policy_result`.

The primary metric is first-time OPA policy pass rate, so `run_seq` is the whole
point — it cannot be reconstructed after the fact, which is why this runs in CI
rather than over history (ADR-002).
"""
import json
import shutil
from pathlib import Path

import pytest

from stdtel.policy_report import build_rows, next_run_seq, write_jsonl

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "policies"
BOTH = ["logging.required_fields", "logging.no_pii"]

pytestmark = pytest.mark.skipif(shutil.which("opa") is None, reason="opa not installed")

COMPLIANT = (
    "import logging\n"
    "logger = logging.getLogger(__name__)\n"
    "logger.info('request', extra={'timestamp': t, 'level': lvl, 'service': svc,\n"
    "                             'trace_id': tid, 'span_id': sid, 'message': msg})\n"
)


def tree(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "app" / "main.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return tmp_path


# --- run_seq: first-time pass is the metric ---

def test_first_run_is_seq_one():
    assert next_run_seq(0) == 1


def test_a_rerun_does_not_overwrite_the_first():
    assert next_run_seq(1) == 2
    assert next_run_seq(7) == 8


def test_negative_prior_count_is_rejected():
    """A bad count would silently mislabel a re-run as first-time."""
    with pytest.raises(ValueError):
        next_run_seq(-1)


# --- the rows themselves ---

def test_rows_match_the_policy_result_schema():
    from tests.test_warehouse import schema_columns
    rows = build_rows(ROOT / "eval" / "fixtures" / "fastapi-min", BOTH, POLICIES, "o/r#1", 1)
    assert rows
    for row in rows:
        assert set(row) == set(schema_columns("policy_result"))


def test_one_row_per_policy_even_when_they_disagree(tmp_path):
    leaky = COMPLIANT.replace("'message': msg}", "'message': msg, 'password': pw}")
    rows = build_rows(tree(tmp_path, leaky), BOTH, POLICIES, "o/r#2", 1)
    by_policy = {r["policy_id"]: r["passed"] for r in rows}
    assert by_policy == {"logging.required_fields": True, "logging.no_pii": False}


def test_run_seq_and_pr_id_are_carried_verbatim(tmp_path):
    rows = build_rows(tree(tmp_path, COMPLIANT), BOTH, POLICIES, "owner/repo#42", 3)
    assert {r["run_seq"] for r in rows} == {3}
    assert {r["pr_id"] for r in rows} == {"owner/repo#42"}


def test_evaluated_at_is_utc_iso8601(tmp_path):
    import datetime as dt
    rows = build_rows(tree(tmp_path, COMPLIANT), BOTH, POLICIES, "o/r#3", 1)
    parsed = dt.datetime.fromisoformat(rows[0]["evaluated_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_unevaluable_policy_fails_rather_than_vanishing(tmp_path):
    """An absent row and a failing row must not look alike (ADR-005)."""
    rows = build_rows(tree(tmp_path, COMPLIANT), ["logging.does_not_exist"], POLICIES, "o/r#4", 1)
    assert len(rows) == 1
    assert rows[0]["passed"] is False


# --- the contract with the loader ---

def test_output_round_trips_through_the_delivery_loader(tmp_path):
    """The two halves must agree without transformation."""
    from warehouse.load_delivery import policy_rows
    out = tmp_path / "policy.jsonl"
    written = build_rows(tree(tmp_path, COMPLIANT), BOTH, POLICIES, "o/r#5", 2)
    write_jsonl(out, written)
    loaded = policy_rows(out)
    assert len(loaded) == len(written)
    assert {r["policy_id"] for r in loaded} == {r["policy_id"] for r in written}
    assert {r["run_seq"] for r in loaded} == {2}


def test_jsonl_is_one_object_per_line(tmp_path):
    out = tmp_path / "p.jsonl"
    write_jsonl(out, build_rows(tree(tmp_path, COMPLIANT), BOTH, POLICIES, "o/r#6", 1))
    lines = [l for l in out.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


# --- policy discovery ---

def test_discovers_every_package_under_the_root():
    from stdtel.policy_report import discover_policy_ids
    ids = discover_policy_ids(POLICIES)
    assert "logging.required_fields" in ids
    assert "logging.no_pii" in ids
    assert "telemetry.manifest_valid" in ids


def test_discovery_is_sorted_and_deduplicated(tmp_path):
    from stdtel.policy_report import discover_policy_ids
    (tmp_path / "a.rego").write_text("package b.two\n")
    (tmp_path / "b.rego").write_text("package a.one\n")
    (tmp_path / "c.rego").write_text("package a.one\n")   # same package, two files
    assert discover_policy_ids(tmp_path) == ["a.one", "b.two"]


# --- the CLI ---

def test_cli_writes_a_file_and_reports_failures(tmp_path, capsys):
    from stdtel.policy_report import main
    work = tree(tmp_path, "from fastapi import FastAPI\napp = FastAPI()\n")   # no logging at all
    out = tmp_path / "results.jsonl"
    rc = main(["--pr-id", "o/r#9", "--policies", str(POLICIES), "--workdir", str(work),
               "--out", str(out), "--run-seq", "1",
               "--policy-ids", "logging.required_fields"])
    assert rc == 0
    assert "failed: logging.required_fields" in capsys.readouterr().out
    assert json.loads(out.read_text().strip())["passed"] is False


def test_cli_refuses_a_missing_policy_root(tmp_path, capsys):
    from stdtel.policy_report import main
    rc = main(["--pr-id", "o/r#10", "--policies", str(tmp_path / "absent"),
               "--out", str(tmp_path / "o.jsonl"), "--run-seq", "1"])
    assert rc == 2
    assert "no policy root" in capsys.readouterr().err


def test_cli_requires_a_run_seq(tmp_path):
    from stdtel.policy_report import main
    with pytest.raises(SystemExit):
        main(["--pr-id", "o/r#11", "--out", str(tmp_path / "o.jsonl")])
