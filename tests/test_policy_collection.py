"""Collecting the policy artefact — the primary metric's only input.

`ci.yml` has uploaded a `policy-results` artefact on every pull-request run
since the job was written, and nothing ever collected it. The metric the project
exists to compute had no input while every CI run went green (#21). These tests
are about the collection step existing, reporting honestly, and being wired into
the automation that runs it.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from warehouse import fetch_policy_results as fpr

ROOT = Path(__file__).resolve().parents[1]


def _run(run_id, event="pull_request"):
    return {"databaseId": run_id, "event": event, "conclusion": "success",
            "headBranch": "x", "createdAt": "2026-09-16T00:00:00Z"}


def test_only_pull_request_runs_are_considered(monkeypatch):
    """The job is guarded by `if: github.event_name == 'pull_request'`, so a push
    run produces nothing. Counting it as a miss would make the report look worse
    than reality and send someone hunting a problem that is not there."""
    monkeypatch.setattr(fpr, "_gh", lambda *a, **k: json.dumps(
        [_run(1), _run(2, "push"), _run(3, "schedule")]))
    assert [r["databaseId"] for r in fpr.pr_runs("o/r")] == [1]


def test_a_duplicate_result_is_collapsed_not_counted_twice(monkeypatch, tmp_path):
    """Re-running a CI run re-uploads the artefact. The same
    (pr_id, policy_id, run_seq) twice is one measurement, not two."""
    row = {"pr_id": "o/r#1", "policy_id": "p.a", "run_seq": 1, "passed": True,
           "evaluated_at": "2026-09-16T00:00:00Z"}
    monkeypatch.setattr(fpr, "pr_runs", lambda *a, **k: [_run(1), _run(2)])
    monkeypatch.setattr(fpr, "artefact_state", lambda *a, **k: "present")
    monkeypatch.setattr(fpr, "download", lambda *a, **k: [dict(row)])
    rows, report = fpr.collect("o/r", workdir=tmp_path)
    assert len(rows) == 1
    assert report["duplicates_collapsed"] == 1
    assert report["rows"] == 1


def test_an_expired_artefact_is_reported_not_silently_missing(monkeypatch, tmp_path, capsys):
    """An expired artefact is data that is gone for good — GitHub deletes them
    after 90 days. The row count alone cannot show that, and a pass rate
    computed over what survived is worth less than it looks (ADR-005)."""
    monkeypatch.setattr(fpr, "pr_runs", lambda *a, **k: [_run(1), _run(2)])
    monkeypatch.setattr(fpr, "artefact_state", lambda repo, rid: "expired" if rid == 1 else "present")
    monkeypatch.setattr(fpr, "download", lambda *a, **k: [
        {"pr_id": "o/r#2", "policy_id": "p.a", "run_seq": 1, "passed": True}])
    rows, report = fpr.collect("o/r", workdir=tmp_path)
    assert report["artefact_expired"] == 1 and report["rows"] == 1

    monkeypatch.setattr(fpr, "collect", lambda *a, **k: (rows, report))
    fpr.main(["--repo", "o/r", "--out", str(tmp_path / "out.jsonl")])
    err = capsys.readouterr().err
    assert "EXPIRED" in err and "90 days" in err


def test_a_run_with_no_artefact_is_a_hole_not_a_failure(monkeypatch, tmp_path, capsys):
    """A PR with no policy result is missing data. Treating it as a failing
    result would invent an observation, and treating it as nothing would hide
    that the metric does not cover that PR."""
    monkeypatch.setattr(fpr, "pr_runs", lambda *a, **k: [_run(1)])
    monkeypatch.setattr(fpr, "artefact_state", lambda *a, **k: "absent")
    rows, report = fpr.collect("o/r", workdir=tmp_path)
    assert rows == [] and report["artefact_absent"] == 1

    monkeypatch.setattr(fpr, "collect", lambda *a, **k: (rows, report))
    assert fpr.main(["--repo", "o/r", "--out", str(tmp_path / "out.jsonl")]) == 1
    err = capsys.readouterr().err
    assert "hole in the metric" in err
    assert "no policy results collected" in err


def test_collecting_nothing_is_a_failure_not_a_quiet_success(monkeypatch, tmp_path):
    """Exit 0 with an empty file is how this went unnoticed for weeks."""
    monkeypatch.setattr(fpr, "collect", lambda *a, **k: ([], {
        "runs_inspected": 0, "artefact_present": 0, "artefact_expired": 0,
        "artefact_absent": 0, "download_failed": 0, "rows": 0, "duplicates_collapsed": 0}))
    assert fpr.main(["--repo", "o/r", "--out", str(tmp_path / "out.jsonl")]) == 1


def test_run_seq_is_never_recomputed_here():
    """ADR-002: run_seq is stamped in CI at the moment of the run. After the
    fact, re-runs and retries make the true ordering unrecoverable, and
    'first-time pass' is the entire metric."""
    source = Path(fpr.__file__).read_text()
    assert "run_seq" in source
    assert not re.search(r"run_seq\s*[\"']?\s*\]?\s*=\s*\d", source), \
        "the collector must carry run_seq through, never assign one"


# --- wired into the automation, or it will not happen ------------------------

def test_the_scheduled_loader_collects_the_artefact():
    """The whole point of #21: this step existing is not enough if nothing runs
    it on a schedule."""
    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
    command = " ".join(str(x) for x in compose["services"]["loader"]["command"])
    assert "fetch_policy_results" in command
    assert "--policy-results" in command, "fetching it and not passing it changes nothing"


def test_the_loader_container_has_the_tool_the_delivery_half_shells_out_to():
    """`load_delivery` and the collector both run `gh`, and python:slim has
    neither gh nor curl. It was wired in before that was noticed, so the
    delivery half could never have run in the container and said only
    'delivery failed' when it didn't."""
    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
    loader = compose["services"]["loader"]
    command = " ".join(str(x) for x in loader["command"])
    assert "cli/cli/releases" in command, "the container must install gh"
    assert "GH_TOKEN" in loader["environment"], "gh reads GH_TOKEN, not GITHUB_TOKEN"


def test_make_load_passes_the_artefact_to_the_delivery_loader():
    makefile = (ROOT / "Makefile").read_text()
    assert "fetch_policy_results" in makefile
    assert "--policy-results" in makefile


@pytest.mark.parametrize("target", ["load", "load-delivery", "load-watch"])
def test_the_load_targets_are_declared_phony(target):
    """`load` is also a plausible file name; an undeclared target stops working
    the day someone creates one."""
    makefile = (ROOT / "Makefile").read_text()
    phony = next(l for l in makefile.splitlines() if l.startswith(".PHONY:"))
    assert target in phony
