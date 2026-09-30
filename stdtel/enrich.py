"""Resource enrichment: join keys derived from git (ticket id, repo, team)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from stdtel.identity import developer_hash

#: A ticket key begins a branch name or a branch segment. Anchoring on `\b`
#: instead looked equivalent and was not: `dependabot/github_actions/actions/
#: upload-artifact-7` upper-cased contains `ARTIFACT-7` after a hyphen, so every
#: Dependabot branch stamped a phantom ticket on every span it produced (#62).
#: The delivery loader carries the same expression deliberately — `warehouse/`
#: is standalone and imports nothing from this package — and
#: `tests/test_delivery_tickets.py` asserts the two agree, because the ADR-002
#: join is exactly a comparison between what this stamps and what that parses.
TICKET = re.compile(r"(?:^|/)([A-Z][A-Z0-9]{1,9}-\d{1,6})")


def _git(args: list[str], cwd: Path | None) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def current_branch(cwd: Path | None = None) -> str:
    """The branch the working tree is on *now*, or "" if git could not be read.

    Split out of `resource_attributes` because the branch is the one input there
    that is not session-scoped (#77). Repo, team, user hash and harness are fixed
    for a session's life; the branch is not, and a loop skill changes it every
    iteration.

    The empty string is load-bearing. "no ticket on this branch" is an
    observation and must be recorded as `unattributed`; "git did not answer" is
    the absence of one, and the caller keeps what it already had rather than
    overwriting a good value with a guess (ADR-005).
    """
    return os.environ.get("STDTEL_BRANCH") or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def ticket_from_branch(branch: str) -> str:
    """The ticket key a branch is named for, or 'unattributed'.

    Must stay identical to `warehouse.load_delivery.ticket_from_branch`: one
    stamps `std.ticket.id` on a span, the other derives `ticket_id` from a PR,
    and ADR-002 joins the two on equality. A difference between them does not
    fail anywhere — it just silently stops rows matching.
    """
    m = TICKET.search((branch or "").upper())
    return m.group(1) if m else "unattributed"


# Copilot's native hook dialect is camelCase; its "VS Code compatible" dialect
# is snake_case and indistinguishable from Claude Code's. These keys are only
# positive evidence of Copilot, never of Claude Code.
COPILOT_ONLY_KEYS = (("sessionId", "session_id"), ("toolName", "tool_name"),
                     ("transcriptPath", "transcript_path"))


def detect_harness(payload: dict | None = None) -> str | None:
    """Harness inferred from the hook payload, or None when it cannot be told.

    VS Code Copilot reads `~/.claude/settings.json` and `.claude/settings.json`
    for hooks, so Copilot events arrive through hooks registered for Claude Code
    and would otherwise be stamped `std.harness=claude-code` — silently
    corrupting the cross-harness comparison this project exists to make.
    camelCase keys prove Copilot. The snake_case dialect cannot be told apart
    from the payload alone: set STDTEL_HARNESS in that hook's own `env` block.
    """
    if not payload:
        return None
    for camel, snake in COPILOT_ONLY_KEYS:
        if camel in payload and snake not in payload:
            return "copilot"      # vscode vs cli needs STDTEL_HARNESS to say
    return None


_URL_REMOTE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", re.I)
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def _remote_parts(remote: str) -> tuple[str, str]:
    """(host, path) of a git remote, or ("", remote) when it is neither form."""
    r = (remote or "").strip().rstrip("/")
    r = re.sub(r"\.git$", "", r).rstrip("/")
    m = _URL_REMOTE.match(r) or _SCP_REMOTE.match(r)
    return (m.group(1), m.group(2)) if m else ("", r)


def repo_id(remote: str) -> str:
    """One repository, however it was cloned: `host/owner/name`, lower-cased.

    ADR-013. ssh and https remotes of one repository must be one identity, or
    the same work splits in two depending on how it was cloned; the host is part
    of it, so a GitLab project is never the GitHub one with the same path. The
    warehouse loader carries its own copy, held equal by a test.
    """
    host, path = _remote_parts(remote)
    if not path:
        return ""
    return f"{host}/{path}".lower() if host else path.lower()


def branch_hash(repo: str, branch: str) -> str:
    """ADR-013's join key: an opaque identity for one branch of one repository.

    A hash rather than the name, because a branch name is free text and can
    carry a customer or project into a shared trace backend; the same scheme as
    `std.user.hash`. Empty when either half is unknown, so repository-less
    sessions do not all share one real-looking value.
    """
    from stdtel.identity import _digest
    if not repo or not branch:
        return ""
    return _digest("stdtel-branch", f"{repo}\n{branch}")


def remote_url(cwd: Path | None = None) -> str:
    return os.environ.get("STDTEL_REPO") or _git(["config", "--get", "remote.origin.url"], cwd)


def resource_attributes(cwd: Path | None = None, payload: dict | None = None) -> dict:
    branch = current_branch(cwd)
    remote = remote_url(cwd)
    _host, path = _remote_parts(remote)
    return {
        # ADR-013: no ticket on the wire. The join to delivery data is this hash
        # plus a time window, confirmed by commit patch-ids.
        "std.branch.hash": branch_hash(repo_id(remote), branch or ""),
        # owner/name, not the bare name, which collides across organisations
        "std.repo": path or "unknown",
        "std.team": os.environ.get("STDTEL_TEAM", "unknown"),
        "std.harness": detect_harness(payload) or os.environ.get("STDTEL_HARNESS", "claude-code"),
        "std.harness.mode": os.environ.get("STDTEL_HARNESS_MODE", "agent"),
        # Pseudonymous, and already hashed when it leaves the process. The
        # collector derived this from user.email, which nothing ever set, so
        # distinct_users read 0 for every skill at every volume (#43) — and the
        # "5+ developers" half of the reporting floor was measured by it.
        "std.user.hash": developer_hash(),
    }


def git_head(cwd: Path | None = None) -> str:
    return _git(["rev-parse", "HEAD"], cwd)


def new_commit_patch_ids(cwd: Path | None, since: str) -> tuple[list[str], str]:
    """ADR-013 commit evidence: patch-ids of commits this session made since `since`.

    Returns (patch_ids, head). The first call of a session passes no `since`
    and gets nothing back but the head to start from, so a commit already on
    the branch is never claimed. Only non-merge commits authored by the local
    git identity count: pulling a teammate's work onto the branch must not claim
    it. Patch-ids, not SHAs, because a rebase keeps the diff and loses the SHA —
    all seven of PR #87's SHAs were lost that way, their patch-ids were not.

    Everything is local git; nothing here knows which forge the repository uses.
    """
    head = git_head(cwd)
    if not head or not since or since == head:
        return [], head or since
    email = _git(["config", "--get", "user.email"], cwd)
    if not email:
        return [], head
    try:
        log = subprocess.run(
            ["git", "log", "-p", "--no-merges", "--reverse", "--no-color",
             f"--author=<{re.escape(email)}>", f"{since}..{head}"],
            cwd=cwd, capture_output=True, text=True, check=True).stdout
        if not log.strip():
            return [], head
        ids = subprocess.run(["git", "patch-id", "--stable"], cwd=cwd, input=log,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        # `since` gone after a rewrite, or git unreadable: keep the old mark
        # rather than skip ahead past commits never reported
        return [], since
    return [line.split()[0] for line in ids.splitlines() if line.strip()], head
