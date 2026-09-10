"""Resource enrichment: join keys derived from git (ticket id, repo, team)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")


def _git(args: list[str], cwd: Path | None) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def ticket_from_branch(branch: str) -> str:
    m = TICKET.search(branch.upper())
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
    branch = os.environ.get("STDTEL_BRANCH") or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote = os.environ.get("STDTEL_REPO") or _git(["config", "--get", "remote.origin.url"], cwd)
    repo = re.sub(r"\.git$", "", remote.rsplit("/", 1)[-1]) if remote else "unknown"
    return {
        "std.ticket.id": ticket_from_branch(branch or ""),
        "std.repo": repo,
        "std.team": os.environ.get("STDTEL_TEAM", "unknown"),
        "std.harness": detect_harness(payload) or os.environ.get("STDTEL_HARNESS", "claude-code"),
        "std.harness.mode": os.environ.get("STDTEL_HARNESS_MODE", "agent"),
    }
