"""Render hook configuration bound to *this* installation's absolute path.

Hook processes get a non-login `sh -c` and inherit whatever PATH launched the
harness, so a bare `stdtel-hook` is not resolvable when a version manager (mise,
asdf, pyenv) or an activated venv is what put it on PATH. Every comparable tool
solves this the same way — pre-commit bakes `sys.executable` into the generated
git hook at install time — so we resolve the absolute path here and write it in.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

HOOK_NAME = "stdtel-hook"
# What the *shipped plugin* manifests invoke. A distributed manifest cannot know
# the install path, and a bare name does not resolve in a hook's `sh -c` — that
# failed with "command not found" on every tool call (issue #13). bin/stdtel-hook
# resolves the real CLI at run time and exits 0 silently when it is absent.
PLUGIN_LAUNCHER = "${CLAUDE_PLUGIN_ROOT}/bin/stdtel-hook"

# (stdtel event, Claude Code event, tool matcher, Copilot event)
# PreToolUse stays matched to Skill: it only opens skill windows, and firing it on
# every tool would be pure latency. PostToolUse is unmatched because tool-call
# failure rate needs every tool, and the non-Skill path is a counter increment.
EVENTS = (
    ("session-start",         "SessionStart",       None,    "sessionStart"),
    ("pre-tool-use",          "PreToolUse",         "Skill", "preToolUse"),
    ("post-tool-use",         "PostToolUse",        None,    "postToolUse"),
    ("post-tool-use-failure", "PostToolUseFailure", None,    "postToolUseFailure"),
    ("stop",                  "Stop",               None,    "agentStop"),
)


class InstallError(RuntimeError):
    pass


def hook_binary() -> Path:
    """Absolute path to the stdtel-hook belonging to this installation.

    Prefer the console script beside the running interpreter — for `uv tool
    install` and pipx that is the tool's own bin directory — and only then fall
    back to PATH, which may resolve to a different install.
    """
    # NOT .resolve(): in a venv sys.executable is a symlink to the base
    # interpreter, and resolving it lands in the base install's bin/, not the
    # venv's — where the console script we want does not exist.
    sibling = Path(sys.executable).parent / HOOK_NAME
    if sibling.is_file():
        return sibling
    found = shutil.which(HOOK_NAME)
    if found:
        return Path(found).resolve()
    raise InstallError(
        f"{HOOK_NAME} not found beside {sys.executable} or on PATH; "
        f"install with `uv tool install stdtel` (or `pipx install stdtel`) first")


def _entry(binary: Path, event: str, exec_form: bool, env: dict | None, timeout: int | None) -> dict:
    """One hook entry.

    Shell form (default) is a single quoted absolute path — the shape Claude Code
    has always accepted. Exec form skips `sh -c` and saves ~3ms per call, but is
    opt-in because we have not executed it against a live harness.
    """
    if exec_form:
        entry: dict = {"type": "command", "command": str(binary), "args": [event]}
    else:
        entry = {"type": "command", "command": f"{shlex.quote(str(binary))} {event}"}
    if timeout is not None:
        entry["timeout"] = timeout
    if env:
        entry["env"] = dict(env)
    return entry


def claude_hooks(binary: "Path | str", exec_form: bool = False) -> dict:
    hooks: dict = {}
    for event, cc_event, matcher, _ in EVENTS:
        group: dict = {"hooks": [_entry(binary, event, exec_form, None, None)]}
        if matcher:
            group["matcher"] = matcher
        hooks[cc_event] = [group]
    return {"hooks": hooks}


def copilot_hooks(binary: "Path | str", harness: str = "copilot-vscode",
                  exec_form: bool = False) -> dict:
    """Copilot hook config.

    STDTEL_HARNESS is set per hook on purpose. Copilot also reads
    `~/.claude/settings.json`, and its snake_case dialect is indistinguishable
    from Claude Code's in the payload, so this env block is the only thing that
    keeps Copilot activity from being recorded as claude-code.
    """
    hooks: dict = {}
    for event, _, matcher, cop_event in EVENTS:
        entry = _entry(binary, event, exec_form, {"STDTEL_HARNESS": harness}, None)
        group: dict = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        hooks[cop_event] = [group]
    return {"version": 1, "disableAllHooks": False, "hooks": hooks}


def merge_settings(target: Path, block: dict) -> dict:
    """Merge hooks/env into an existing settings file without clobbering it."""
    current = {}
    if target.is_file():
        try:
            current = json.loads(target.read_text() or "{}")
        except json.JSONDecodeError as e:
            raise InstallError(f"{target} is not valid JSON: {e}") from e
    for key, value in block.items():
        if isinstance(value, dict):
            current.setdefault(key, {}).update(value)
        else:
            current[key] = value
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2) + "\n")
    return current


def _dump(obj: dict) -> str:
    return json.dumps(obj, indent=2)


# Said at the moment it can still save someone hours. Hook configuration is read
# at session start, so a session that is already open runs no hooks for the rest
# of its life — and hooks exit 0 in silence, so it looks exactly like a working
# install producing no work (#44).
RESTART_NOTICE = ("\nRestart Claude Code to pick these up. A session that is already open keeps "
                  "the\nhook configuration it started with, and will record nothing.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stdtel-install", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hooks = sub.add_parser("hooks", help="print hook config for a harness")
    p_hooks.add_argument("--harness", default="claude-code",
                         choices=["claude-code", "copilot-vscode", "copilot-cli"])
    p_hooks.add_argument("--exec-form", action="store_true",
                         help="use command+args instead of a shell string (unverified upstream)")

    p_set = sub.add_parser("settings", help="merge hooks into a Claude Code settings.json")
    p_set.add_argument("--path", type=Path, default=Path.home() / ".claude" / "settings.json")
    p_set.add_argument("--exec-form", action="store_true")
    p_set.add_argument("--dry-run", action="store_true")

    p_where = sub.add_parser("where", help="print the resolved absolute hook path")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        binary = hook_binary()
    except InstallError as e:
        print(f"stdtel-install: {e}", file=sys.stderr)
        return 1

    if args.cmd == "where":
        print(binary)
    elif args.cmd == "hooks":
        block = (claude_hooks(binary, args.exec_form) if args.harness == "claude-code"
                 else copilot_hooks(binary, args.harness, args.exec_form))
        print(_dump(block))
    elif args.cmd == "settings":
        block = claude_hooks(binary, args.exec_form)
        if args.dry_run:
            print(_dump(block))
        else:
            merge_settings(args.path, block)
            print(f"wrote {len(block['hooks'])} hook events to {args.path} -> {binary}")
            print(RESTART_NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
