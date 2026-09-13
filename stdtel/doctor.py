"""`stdtel doctor` — make a silently-broken install visible.

Hooks exit 0 so telemetry never blocks the developer. The cost of that rule is
that a broken install looks exactly like a working one: no spans and a healthy
session are indistinguishable from the outside. Four install-time failures in
this project's history were found by a person using it, none by its tests.

Every check reports a verdict, what was observed, and **the remedy** — the part a
diagnostic usually leaves out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 3


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str = ""


def _git_branch() -> str:
    """Current branch, or empty when git cannot answer."""
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                             # noqa: BLE001 - not a git repo, no git, etc.
        return ""


def hook_resolvable() -> Check:
    """Can a hook actually run `stdtel-hook`?

    Checked the way a hook resolves it — a non-login `sh -c` — not the way an
    interactive shell does. Shipping the bare name failed exactly here, with
    "command not found" on every tool call (#13), while it resolved fine in a
    terminal because a version manager had it on PATH.
    """
    try:
        # /bin/sh by absolute path: a diagnostic for a broken PATH must not need
        # a working PATH to run, and /bin/sh is what a hook is given anyway
        r = subprocess.run(["/bin/sh", "-c", "command -v stdtel-hook"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        found = r.stdout.strip()
    except Exception as e:                        # noqa: BLE001 - diagnostics must not raise
        return Check("hook resolvable", False, f"could not probe: {e}",
                     "check that `sh` is available")
    if found:
        return Check("hook resolvable", True, f"sh -c finds {found}")
    which = shutil.which("stdtel-hook")
    detail = ("not on the PATH a hook gets (sh -c); "
              + (f"an interactive shell finds {which}" if which else "not on this shell's PATH either"))
    return Check("hook resolvable", False, detail,
                 "uv tool install stdtel, then `stdtel-install settings` to write an absolute path")


def hooks_registered() -> Check:
    """Registered once, in exactly one place.

    Plugin hooks and settings hooks do not deduplicate against each other, so
    both firing means every window is counted twice.
    """
    import json
    settings = Path.home() / ".claude" / "settings.json"
    in_settings = False
    if settings.is_file():
        try:
            # Look for OUR hooks specifically. Testing `bool(hooks)` reported
            # "registered in settings AND as a plugin" to anyone with hooks of
            # their own — a confidently wrong warning, in the tool meant to
            # diagnose confidently wrong behaviour.
            raw = json.loads(settings.read_text() or "{}").get("hooks") or {}
            in_settings = "stdtel-hook" in json.dumps(raw)
        except json.JSONDecodeError:
            return Check("hooks registered", False, f"{settings} is not valid JSON",
                         "fix or remove the file, then run `stdtel-install settings`")
    plugin = (Path.home() / ".claude" / "plugins" / "installed_plugins.json")
    in_plugin = plugin.is_file() and "stdtel" in plugin.read_text()
    if in_settings and in_plugin:
        return Check("hooks registered", False, "registered in settings AND as a plugin",
                     "remove the hooks block from ~/.claude/settings.json; plugin hooks and settings "
                     "hooks both fire, double-counting every skill window")
    if in_settings or in_plugin:
        return Check("hooks registered", True,
                     "via settings.json" if in_settings else "via the plugin")
    return Check("hooks registered", False, "no stdtel hooks found",
                 "run `stdtel-install settings`, or install the plugin")


def ticket_key() -> Check:
    """Does this branch yield a ticket key?

    Only fixable now: a branch renamed tomorrow does not retroactively attribute
    today's work, and unattributed sessions are excluded from outcome analysis.
    """
    from stdtel.enrich import ticket_from_branch
    branch = os.environ.get("STDTEL_BRANCH") or _git_branch()
    key = ticket_from_branch(branch or "")
    if key == "unattributed":
        return Check("ticket key", False,
                     f"branch {branch or '(unknown)'!r} yields 'unattributed'",
                     "rename the branch to carry a ticket key, e.g. feature/PLAT-42-thing — "
                     "this work is excluded from outcome analysis until it does")
    return Check("ticket key", True, f"{branch} -> {key}")


def catalogue_ok() -> Check:
    """Can the catalogue be found, and does it contain anything?

    An empty or unfindable catalogue does not fail loudly: skills are simply
    recorded as `unversioned`, with no standard_id and no policy_ids.
    """
    from stdtel.hooks.cli import _catalogue, skills_roots
    roots = skills_roots()
    cat = _catalogue()
    if not roots:
        return Check("skill catalogue", False, "no catalogue root exists",
                     "symlink skills into ~/.claude/skills, or set STDTEL_SKILLS_ROOT")
    if not cat:
        return Check("skill catalogue", False,
                     f"{len(roots)} root(s) searched, 0 skills found",
                     "skills will record as `unversioned` with no standard_id; check "
                     "STDTEL_SKILLS_ROOT and run `stdtel-validate` on it")
    return Check("skill catalogue", True, f"{len(cat)} skill(s) across {len(roots)} root(s)")


def collector_ok() -> Check:
    """Is anything listening where spans are being sent?

    A dead collector is the quietest failure of all: the exporter times out and
    drops, leaving one stderr line no editor shows.
    """
    import urllib.error
    import urllib.request
    from stdtel.exporter import _endpoint
    endpoint = _endpoint()
    req = urllib.request.Request(endpoint, data=b"{}", method="POST",
                                 headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_S)
        return Check("collector reachable", True, f"{endpoint} answered")
    except urllib.error.HTTPError:
        # any HTTP status means something is listening and speaking OTLP
        return Check("collector reachable", True, f"{endpoint} answered")
    except Exception as e:                        # noqa: BLE001
        return Check("collector reachable", False, f"{endpoint}: {type(e).__name__}",
                     "spans are being dropped right now. Start the stack with `make up`, or point "
                     "STDTEL_OTLP_ENDPOINT at a live collector (OTEL_* cannot reach a hook)")


def recent_state() -> Check:
    """Has a hook actually run recently? Registration alone proves nothing."""
    from stdtel.state import state_dir
    try:
        files = sorted(state_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:                        # noqa: BLE001
        return Check("hooks running", False, f"cannot read state dir: {e}", "check STDTEL_STATE_DIR")
    if not files:
        return Check("hooks running", False, "no session state has ever been written",
                     "the hooks are registered but not firing; run `stdtel-install where` and "
                     "invoke stdtel-hook by hand to see the error")
    import datetime as dt
    newest = dt.datetime.fromtimestamp(files[0].stat().st_mtime)
    return Check("hooks running", True, f"last session state {newest:%Y-%m-%d %H:%M}")


CHECKS = (hook_resolvable, hooks_registered, ticket_key, catalogue_ok, collector_ok, recent_state)


def check_all() -> list[Check]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:                    # noqa: BLE001 - a diagnostic must never crash
            out.append(Check(fn.__name__, False, f"check itself failed: {e}",
                             "this is a bug in stdtel doctor"))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stdtel-doctor", description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", "-q", action="store_true", help="only show problems")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    checks = check_all()
    failed = [c for c in checks if not c.ok]
    for c in checks:
        if c.ok and a.quiet:
            continue
        mark = "ok  " if c.ok else "FAIL"
        print(f"  {mark}  {c.name:<20} {c.detail}")
        if not c.ok and c.remedy:
            print(f"        -> {c.remedy}")
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
