"""`std.ticket.id` must follow the branch, not the session (#77).

It was derived from the git branch once, in `session_start`, stored on the
session state and reused unchanged by every later hook. Nothing recomputed it.

The ticket is the join key for all delivery data (ADR-002), so a stale one does
not fail: it joins cleanly to the *wrong* pull request, and `scorecard.sql` then
attributes one ticket's cost and policy outcome to another. That is a confident
wrong answer rather than missing data, which ADR-005 treats as the more serious
failure, and it cannot be repaired afterwards because the branch at the moment
of the span is gone.

The workload that breaks it hardest is the one this repo exists to measure: a
loop skill walking an epic checks out a new branch every iteration.
"""
import subprocess
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks


def tickets(exporter) -> set:
    return {s.resource.attributes.get("std.ticket.id") for s in exporter.get_finished_spans()}


def skill(sid, tool_use_id, prompt_id):
    """One skill activation, so the Stop below has a span to carry a ticket on.

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
    return tickets(exp)


def test_the_ticket_follows_the_branch_within_one_session(tmp_path, monkeypatch):
    """The defect. One session, two branches, and every span carried the first."""
    sid = "ticket-follows"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-1-first")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {"STDTEL-1"}

    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-2-second")
    skill(sid, "t2", "p2")
    assert run_stop(sid, tmp_path) == {"STDTEL-2"}, \
        "the second iteration's spans still carry the first iteration's ticket"


def test_a_branch_with_no_ticket_key_is_recorded_as_unattributed(tmp_path, monkeypatch):
    """"This branch names no ticket" is an observation and must be recorded as
    one. Keeping the previous ticket here is exactly the bug."""
    sid = "ticket-none"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-1-first")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    run_stop(sid, tmp_path)

    monkeypatch.setenv("STDTEL_BRANCH", "main")
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {"unattributed"}


def test_an_unreadable_branch_keeps_the_last_known_ticket(tmp_path, monkeypatch):
    """Absence of an observation is not an observation of absence. Overwriting a
    good ticket because git happened not to answer would turn a transient
    failure into permanent, unrecoverable data loss."""
    sid = "ticket-nogit"
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-1-first")
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    run_stop(sid, tmp_path)

    monkeypatch.delenv("STDTEL_BRANCH")          # and tmp_path is not a git repo
    skill(sid, "t1", "p1")
    assert run_stop(sid, tmp_path) == {"STDTEL-1"}


def test_the_ticket_follows_a_real_git_checkout(tmp_path, monkeypatch):
    """The env override is a test seam; this is the path a developer is on.

    Without it the suite would pass while `current_branch` asked git the wrong
    question, which is the shape of the three loader bugs in #55.
    """
    monkeypatch.delenv("STDTEL_BRANCH", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    git("init", "-q", "-b", "STDTEL-11-one")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "init")

    sid = "ticket-realgit"
    hooks.session_start({"session_id": sid, "cwd": str(repo)})
    skill(sid, "t0", "p0")
    assert run_stop(sid, repo) == {"STDTEL-11"}

    git("checkout", "-q", "-b", "STDTEL-12-two")
    skill(sid, "t1", "p1")
    assert run_stop(sid, repo) == {"STDTEL-12"}


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
    assert teams == {"payments"}, "team is session-scoped; only the ticket is not"
