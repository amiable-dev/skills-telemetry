"""`std.branch.hash` must follow the branch, not the session (#77, ADR-013).

This was `std.ticket.id`, derived once in `session_start` and reused by every
later hook. A join key that goes stale does not fail: it joins cleanly to the
*wrong* change request, which ADR-005 treats as worse than missing data, and it
cannot be repaired afterwards because the branch at the moment of the span is
gone. ADR-013 replaced the ticket with a branch identity; the rule carries over.

The workload that breaks it hardest is the one this repo exists to measure: a
loop skill walking an epic checks out a new branch every iteration.
"""
import subprocess
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.enrich import branch_hash
from stdtel.hooks import cli as hooks

REPO = "git@github.com:a/b.git"


def key(branch: str) -> str:
    return branch_hash("github.com/a/b", branch)


@pytest.fixture(autouse=True)
def remote(monkeypatch):
    monkeypatch.setenv("STDTEL_REPO", REPO)


def hashes(exporter) -> set:
    return {s.resource.attributes.get("std.branch.hash") for s in exporter.get_finished_spans()}


def skill(sid, tool_use_id, prompt_id):
    """One skill activation, so the Stop below has a span to carry the hash on.

    A Stop with nothing to report emits nothing at all, which made the first
    draft of two of these tests assert against an empty set and pass vacuously.
    """
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tool_use_id,
                        "prompt_id": prompt_id, "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tool_use_id,
                         "prompt_id": prompt_id})


def run_stop(sid, tmp_path) -> set:
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(tmp_path)}, exporter=exp)
    return hashes(exp)


def test_the_branch_hash_follows_the_branch_within_one_session(tmp_path, monkeypatch):
    """The defect. One session, two branches, and every span carried the first."""
    sid = "ticket-follows"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-1-first")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {key("feature/STDTEL-1-first")}

    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-2-second")
    skill(sid, "t2", "p2")
    assert run_stop(sid, tmp_path) == {key("feature/STDTEL-2-second")}, \
        "the second iteration's spans still carry the first iteration's branch"


def test_a_branch_with_no_ticket_key_still_has_an_identity(tmp_path, monkeypatch):
    """The point of ADR-013. Under the ticket regex this was `unattributed` and
    excluded from every outcome; any branch name now joins."""
    sid = "branch-nokey"
    monkeypatch.setenv("STDTEL_BRANCH", "fix/media-hardening-233")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {key("fix/media-hardening-233")}


def test_an_unreadable_branch_keeps_the_last_known_hash(tmp_path, monkeypatch):
    """Absence of an observation is not an observation of absence. Overwriting a
    good hash because git happened not to answer would turn a transient
    failure into permanent, unrecoverable data loss."""
    sid = "ticket-nogit"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-1-first")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    run_stop(sid, tmp_path)

    monkeypatch.delenv("STDTEL_BRANCH")          # and tmp_path is not a git repo
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {key("feature/STDTEL-1-first")}


def test_the_branch_hash_follows_a_real_git_checkout(tmp_path, monkeypatch):
    """The env override is a test seam; this is the path a developer is on.

    Without it the suite would pass while `current_branch` asked git the wrong
    question, which is the shape of the three loader bugs in #55.
    """
    monkeypatch.delenv("STDTEL_BRANCH", raising=False)
    monkeypatch.delenv("STDTEL_REPO", raising=False)      # the real remote, below
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init", "-q", "-b", "STDTEL-11-one")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("remote", "add", "origin", REPO)
    (repo / "f.txt").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "init")

    sid = "ticket-realgit"
    hooks.session_start({"session_id": sid, "cwd": str(repo)})
    skill(sid, "t0", "p0")
    assert run_stop(sid, repo) == {key("STDTEL-11-one")}

    git("checkout", "-q", "-b", "STDTEL-12-two")
    skill(sid, "t1", "p1")
    assert run_stop(sid, repo) == {key("STDTEL-12-two")}


def test_the_session_scoped_fields_are_not_recomputed_per_stop(tmp_path, monkeypatch):
    """Only the branch changes mid-session. Re-deriving repo, team and user hash
    every Stop would add git calls to the hot path for values that cannot move.
    """
    sid = "ticket-scope"
    monkeypatch.setenv("STDTEL_BRANCH", "STDTEL-1-x")
    monkeypatch.setenv("STDTEL_TEAM", "payments")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    monkeypatch.setenv("STDTEL_TEAM", "platform")     # changed after the session began
    skill(sid, "t0", "p0")
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(tmp_path)}, exporter=exp)
    teams = {s.resource.attributes.get("std.team") for s in exp.get_finished_spans()}
    assert teams == {"payments"}, "team is session-scoped; only the branch is not"
