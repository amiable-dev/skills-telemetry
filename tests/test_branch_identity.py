"""ADR-013 capture: a branch identity on every span, commit evidence per turn,
and no ticket on the wire.

The ticket-key regex over branch names made a naming convention a hard
requirement nobody was told about. Measured, it missed 2 of 9 of this repo's own
branches and invented RELEASE-0, ANALYZE-4 and INIT-4. A hash of repo + branch
needs no convention and cannot produce a false match.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel import enrich
from stdtel.hooks import cli as hooks
from stdtel.identity import _digest


# --- repo identity -------------------------------------------------------------------

@pytest.mark.parametrize("remote", [
    "git@github.com:amiable-dev/skills-telemetry.git",
    "https://github.com/amiable-dev/skills-telemetry",
    "https://github.com/amiable-dev/skills-telemetry.git",
    "ssh://git@github.com/amiable-dev/skills-telemetry.git",
    "https://someone@github.com/Amiable-Dev/Skills-Telemetry/",
])
def test_every_spelling_of_one_remote_is_one_repository(remote):
    """ssh and https clones of one repository must hash to one branch identity,
    or the same work splits in two depending on how it was cloned."""
    assert enrich.repo_id(remote) == "github.com/amiable-dev/skills-telemetry"


def test_the_forge_is_part_of_the_identity():
    """A GitLab project with the same path is a different repository."""
    assert enrich.repo_id("git@gitlab.com:group/sub/proj.git") == "gitlab.com/group/sub/proj"
    assert enrich.repo_id("git@gitlab.com:amiable-dev/skills-telemetry.git") != \
        enrich.repo_id("git@github.com:amiable-dev/skills-telemetry.git")


def test_no_remote_is_no_identity():
    assert enrich.repo_id("") == ""


def test_the_branch_hash_uses_the_user_hash_scheme():
    rid = "github.com/amiable-dev/skills-telemetry"
    h = enrich.branch_hash(rid, "fix/media-hardening-233")
    assert h == _digest("stdtel-branch", rid + "\n" + "fix/media-hardening-233")
    assert h != enrich.branch_hash(rid, "fix/media-hardening-234")
    assert h != enrich.branch_hash("github.com/other/repo", "fix/media-hardening-233")


def test_no_repository_or_no_branch_yields_no_hash():
    """An empty identity must not hash to a real-looking value every
    repository-less session would share."""
    assert enrich.branch_hash("", "main") == ""
    assert enrich.branch_hash("github.com/a/b", "") == ""


def test_the_resource_carries_the_branch_hash_and_never_a_ticket(monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_BRANCH", "fix/media-hardening-233")
    monkeypatch.setenv("STDTEL_REPO", "git@github.com:amiable-dev/skills-telemetry.git")
    r = enrich.resource_attributes(tmp_path)
    assert r["std.branch.hash"] == enrich.branch_hash("github.com/amiable-dev/skills-telemetry",
                                                     "fix/media-hardening-233")
    assert "std.ticket.id" not in r
    assert r["std.repo"] == "amiable-dev/skills-telemetry", "the bare name collides across owners"


# --- the per-Stop refresh (#81's rule, carried over) -----------------------------------

def _skill(sid, tid, pid):
    hooks.pre_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tid,
                        "prompt_id": pid, "tool_input": {"skill": "structured-logging"}})
    hooks.post_tool_use({"session_id": sid, "tool_name": "Skill", "tool_use_id": tid, "prompt_id": pid})


def _stop(sid, cwd) -> list:
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(cwd)}, exporter=exp)
    return exp.get_finished_spans()


def test_no_span_carries_a_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_REPO", "git@github.com:a/b.git")
    monkeypatch.setenv("STDTEL_BRANCH", "feature/PLAT-42-thing")
    hooks.session_start({"session_id": "bh-2", "cwd": str(tmp_path)})
    _skill("bh-2", "t1", "p1")
    for s in _stop("bh-2", tmp_path):
        assert "std.ticket.id" not in s.attributes and "std.ticket.id" not in s.resource.attributes


# --- scope unit renamed -------------------------------------------------------------------

def test_branch_is_a_scope_unit_and_ticket_is_refused_by_name():
    """Nothing declares `ticket` today, so the rename is free; refusing the old
    name loudly means a stale declaration cannot quietly stop scoping."""
    from stdtel.manifest import SCOPES, ManifestError
    from tests.test_scope_declaration import manifest
    assert SCOPES == {"turn", "branch"}
    assert manifest(scope="branch").effective_scope() == "branch"
    with pytest.raises(ManifestError) as e:
        manifest(scope="ticket")
    assert "branch" in str(e.value)


def test_a_branch_scope_is_keyed_on_the_branch_hash():
    class St:
        resource = {"std.branch.hash": "abc123"}
    assert hooks._scope_key(St(), "branch") == "abc123"
    assert hooks._scope_key(St(), "turn") == ""


# --- commit evidence -------------------------------------------------------------------------

def git(repo: Path, *args, env=None) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "me@example.com")
    git(r, "config", "user.name", "Me")
    git(r, "remote", "add", "origin", "git@github.com:a/b.git")
    (r / "f.txt").write_text("one\n")
    git(r, "add", "f.txt")
    git(r, "commit", "-q", "-m", "base")
    git(r, "checkout", "-q", "-b", "feature")
    monkeypatch.delenv("STDTEL_BRANCH", raising=False)
    monkeypatch.delenv("STDTEL_REPO", raising=False)
    return r


def _turn_transcript(tmp_path: Path, sid: str) -> Path:
    """Appends one more turn on every call, so each Stop has a turn of its own.

    Rewriting the same single turn made a second Stop emit no turn at all, and a
    test of "reported once" then passed without being able to see a repeat.
    """
    import json
    t = tmp_path / f"{sid}.jsonl"
    n = sum(1 for line in t.read_text().splitlines() if '"promptId"' in line) if t.exists() else 0
    with t.open("a") as f:
        f.write(json.dumps({"type": "user", "timestamp": f"2026-09-20T10:0{n}:00.000Z",
                            "promptId": f"p{n + 1}", "message": {"content": []}}) + "\n")
        f.write(json.dumps({"type": "assistant", "timestamp": f"2026-09-20T10:0{n}:01.000Z",
                            "message": {"model": "claude-opus-5", "content": [],
                                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                                  "cache_read_input_tokens": 0,
                                                  "cache_creation_input_tokens": 0}}}) + "\n")
    return t


def _patch_id(repo: Path, sha: str) -> str:
    diff = subprocess.run(["git", "show", sha], cwd=repo, capture_output=True, text=True).stdout
    return subprocess.run(["git", "patch-id", "--stable"], input=diff, capture_output=True,
                          text=True).stdout.split()[0]


def _turn_attrs(tmp_path, sid, repo) -> dict:
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(repo),
                "transcript_path": str(_turn_transcript(tmp_path, sid))}, exporter=exp)
    return next((s.attributes for s in exp.get_finished_spans()
                 if s.attributes.get("std.artefact.kind") == "turn"), {})


def test_a_commit_made_during_the_session_is_carried_as_its_patch_id(tmp_path, repo):
    hooks.session_start({"session_id": "ce-1", "cwd": str(repo)})
    (repo / "f.txt").write_text("two\n")
    git(repo, "commit", "-qam", "work")
    mine = git(repo, "rev-parse", "HEAD")
    attrs = _turn_attrs(tmp_path, "ce-1", repo)
    assert tuple(attrs["std.artefact.commit_patch_ids"]) == (_patch_id(repo, mine),)


def test_a_commit_already_there_at_session_start_is_not_the_sessions(tmp_path, repo):
    hooks.session_start({"session_id": "ce-2", "cwd": str(repo)})
    assert "std.artefact.commit_patch_ids" not in _turn_attrs(tmp_path, "ce-2", repo)


def test_someone_elses_commit_and_a_merge_are_not_the_sessions(tmp_path, repo):
    """Pulling a teammate's work onto the branch must not claim it."""
    hooks.session_start({"session_id": "ce-3", "cwd": str(repo)})
    git(repo, "checkout", "-q", "-b", "theirs", "main")
    (repo / "g.txt").write_text("theirs\n")
    git(repo, "add", "g.txt")
    git(repo, "-c", "user.email=them@example.com", "commit", "-q", "-m", "theirs")
    git(repo, "checkout", "-q", "feature")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "theirs")
    assert "std.artefact.commit_patch_ids" not in _turn_attrs(tmp_path, "ce-3", repo)


def test_each_commit_is_reported_once(tmp_path, repo):
    hooks.session_start({"session_id": "ce-4", "cwd": str(repo)})
    (repo / "f.txt").write_text("two\n")
    git(repo, "commit", "-qam", "work")
    assert _turn_attrs(tmp_path, "ce-4", repo).get("std.artefact.commit_patch_ids")
    second = _turn_attrs(tmp_path, "ce-4", repo)
    assert second.get("std.artefact.kind") == "turn", "the second Stop must have a turn to see a repeat on"
    assert "std.artefact.commit_patch_ids" not in second, \
        "the second Stop re-reported a commit the first already carried"


def test_a_rebased_commit_keeps_its_patch_id(tmp_path, repo):
    """The property the design rests on, checked on real git: the seven SHAs of
    PR #87 were lost to a rebase, their patch-ids were not."""
    (repo / "f.txt").write_text("two\n")
    git(repo, "commit", "-qam", "work")
    before = _patch_id(repo, git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "-q", "main")
    (repo / "h.txt").write_text("unrelated\n")
    git(repo, "add", "h.txt")
    git(repo, "commit", "-q", "-m", "main moves")
    git(repo, "checkout", "-q", "feature")
    git(repo, "rebase", "-q", "main")
    after = _patch_id(repo, git(repo, "rev-parse", "HEAD"))
    assert before == after


# --- the loader's half -----------------------------------------------------------------------

def test_the_loader_writes_one_evidence_row_per_patch_id():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lt_bi", Path(__file__).resolve().parent.parent / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    span = {"name": "std.artefact.activation", "spanId": "s1", "traceId": "t",
            "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000",
            "attributes": [
                {"key": "std.artefact.kind", "value": {"stringValue": "turn"}},
                {"key": "std.prompt.id", "value": {"stringValue": "p1"}},
                {"key": "session.id", "value": {"stringValue": "S"}},
                {"key": "std.artefact.commit_patch_ids",
                 "value": {"arrayValue": {"values": [{"stringValue": "aa"}, {"stringValue": "bb"}]}}}]}
    trace = {"batches": [{"resource": {"attributes": [
        {"key": "std.branch.hash", "value": {"stringValue": "H"}}]}, "scopeSpans": [{"spans": [span]}]}]}
    ev: list = []
    acts, _, _ = mod.collect({"t": 1}, lambda _: trace, evidence=ev)
    assert acts[0]["branch_hash"] == "H"
    assert [(r["patch_id"], r["session_id"], r["prompt_id"], r["branch_hash"]) for r in ev] == [
        ("aa", "S", "p1", "H"), ("bb", "S", "p1", "H")]


def test_evidence_from_a_stop_with_no_turn_waits_for_the_next_turn(tmp_path, repo):
    """A Stop can emit no turn (nothing new in the transcript). Its commits must
    not be dropped: they ride on the next turn instead."""
    hooks.session_start({"session_id": "ce-5", "cwd": str(repo)})
    (repo / "f.txt").write_text("two\n")
    git(repo, "commit", "-qam", "work")
    mine = _patch_id(repo, git(repo, "rev-parse", "HEAD"))
    hooks.stop({"session_id": "ce-5", "cwd": str(repo)}, exporter=InMemorySpanExporter())   # no transcript
    assert tuple(_turn_attrs(tmp_path, "ce-5", repo)["std.artefact.commit_patch_ids"]) == (mine,)
