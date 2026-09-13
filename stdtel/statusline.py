"""A statusline that surfaces data-quality faults while they are still fixable.

Wired via the `statusLine` setting. Claude Code sends session JSON on stdin,
debounces at 300ms and **cancels an in-flight script** when a new update arrives,
so this reads local state only — no catalogue parse in the common path, and never
a network call.

It shows faults you can act on now:

  stdtel ⚠ no ticket · 2 unversioned
  stdtel PLAT-42

An unattributed branch renamed tomorrow does not retroactively attribute today's
PRs, which is why this belongs in front of you rather than in a weekly report.

**It deliberately shows no cost or token total.** The payload offers both.
docs/for-developers.md commits to per-skill analysis rather than individual
measurement, and a live running total of your own spend in your editor reads as
surveillance however it is framed — and invites optimising the number rather than
the work.
"""
from __future__ import annotations

import json
import os
import sys

PREFIX = "stdtel"


def _off() -> bool:
    if os.environ.get("STDTEL_STATUSLINE", "").strip().lower() in ("off", "0", "false", "no"):
        return True
    from stdtel.hooks.cli import disabled
    return disabled()


def _uncatalogued(state) -> int:
    """Open skill windows whose name the catalogue does not know.

    Only pays for the catalogue when there are windows to check, which keeps the
    common render free of a PyYAML parse.
    """
    windows = getattr(state, "windows", None) or []
    if not windows:
        return 0
    try:
        from stdtel.hooks.cli import _catalogue, _resolve
        cat = _catalogue()
        return sum(1 for w in windows if _resolve(w.skill, cat)[0] is None)
    except Exception:                             # noqa: BLE001 - never break the status bar
        return 0


def render(payload: dict) -> str:
    """The line to display. Empty string when there is nothing worth saying."""
    if _off():
        return ""
    try:
        session_id = (payload or {}).get("session_id")
        if not session_id:
            return ""
        from stdtel.state import SessionState
        state = SessionState.load(str(session_id))
    except Exception:                             # noqa: BLE001
        return ""

    ticket = (getattr(state, "resource", None) or {}).get("std.ticket.id")
    faults = []
    if ticket == "unattributed":
        faults.append("no ticket")
    n = _uncatalogued(state)
    if n:
        faults.append(f"{n} unversioned")
    if getattr(state, "last_export_ok", None) is False:
        faults.append("spans dropping")

    if faults:
        return f"{PREFIX} ⚠ " + " · ".join(faults)
    if ticket and ticket != "unattributed":
        return f"{PREFIX} {ticket}"
    return ""


def main(argv: list[str] | None = None) -> int:
    """Always exits 0: a broken statusline must never break the status bar."""
    try:
        payload = json.load(sys.stdin)
    except Exception:                             # noqa: BLE001
        payload = {}
    try:
        line = render(payload)
    except Exception:                             # noqa: BLE001
        line = ""
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
