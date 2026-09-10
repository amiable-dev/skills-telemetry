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


def resource_attributes(cwd: Path | None = None) -> dict:
    branch = os.environ.get("STDTEL_BRANCH") or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    remote = os.environ.get("STDTEL_REPO") or _git(["config", "--get", "remote.origin.url"], cwd)
    repo = re.sub(r"\.git$", "", remote.rsplit("/", 1)[-1]) if remote else "unknown"
    return {
        "std.ticket.id": ticket_from_branch(branch or ""),
        "std.repo": repo,
        "std.team": os.environ.get("STDTEL_TEAM", "unknown"),
        "std.harness": os.environ.get("STDTEL_HARNESS", "claude-code"),
        "std.harness.mode": os.environ.get("STDTEL_HARNESS_MODE", "agent"),
    }
