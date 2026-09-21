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


def resource_attributes(cwd: Path | None = None, payload: dict | None = None) -> dict:
    branch = current_branch(cwd)
    remote = os.environ.get("STDTEL_REPO") or _git(["config", "--get", "remote.origin.url"], cwd)
    repo = re.sub(r"\.git$", "", remote.rsplit("/", 1)[-1]) if remote else "unknown"
    return {
        "std.ticket.id": ticket_from_branch(branch or ""),
        "std.repo": repo,
        "std.team": os.environ.get("STDTEL_TEAM", "unknown"),
        "std.harness": detect_harness(payload) or os.environ.get("STDTEL_HARNESS", "claude-code"),
        "std.harness.mode": os.environ.get("STDTEL_HARNESS_MODE", "agent"),
        # Pseudonymous, and already hashed when it leaves the process. The
        # collector derived this from user.email, which nothing ever set, so
        # distinct_users read 0 for every skill at every volume (#43) — and the
        # "5+ developers" half of the reporting floor was measured by it.
        "std.user.hash": developer_hash(),
    }
