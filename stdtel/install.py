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
import os
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
# SubagentStart/SubagentStop and PostCompact are ADR-009: a sub-agent was the
# largest unmeasured cost in a real session, and compaction the largest single
# event. Both are rare next to PostToolUse and both only touch local state.
# Copilot has no equivalent for either, so those columns are None — the hook is
# simply not registered there rather than registered and silently never firing.
EVENTS = (
    ("session-start",         "SessionStart",       None,    "sessionStart"),
    ("pre-tool-use",          "PreToolUse",         "Skill", "preToolUse"),
    ("post-tool-use",         "PostToolUse",        None,    "postToolUse"),
    ("post-tool-use-failure", "PostToolUseFailure", None,    "postToolUseFailure"),
    ("subagent-start",        "SubagentStart",      None,    None),
    ("subagent-stop",         "SubagentStop",       None,    None),
    ("post-compact",          "PostCompact",        None,    None),
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
        if cop_event is None:
            continue        # no Copilot equivalent; registering one would be a guess
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


def _collector_base() -> str:
    """The collector stdtel's own spans go to, as a base URL."""
    from stdtel.exporter import _endpoint
    return _endpoint().removesuffix("/v1/traces")


def claude_native_env() -> dict:
    """ADR-014 decision 2: Claude Code's own events, and nothing else.

    Absent on purpose: OTEL_METRICS_EXPORTER, because native metrics carry
    session.id as a label (decision 4); and every content flag, above all
    OTEL_LOG_TOOL_DETAILS, which only stdtel-setup offers, with consent, once
    `stdtel-doctor --content-check` has passed on this machine (decisions 12, 13).
    These are Claude Code's own variables, read by Claude Code, so ADR-003's
    scrubbing of OTEL_* from hook processes does not apply.
    """
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": _collector_base(),
        "OTEL_METRICS_INCLUDE_REPOSITORY": "true",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
    }


def copilot_vscode_settings() -> dict:
    """VS Code's Copilot OTel settings (documented 2026-09-16, read 2026-09-30).
    captureContent is written as false rather than left to its default, so the
    intent is visible in the file."""
    return {
        "github.copilot.chat.otel.enabled": True,
        "github.copilot.chat.otel.exporterType": "otlp-http",
        "github.copilot.chat.otel.otlpEndpoint": _collector_base(),
        "github.copilot.chat.otel.captureContent": False,
    }


def copilot_cli_env() -> dict:
    """The Copilot CLI's equivalents. COPILOT_OTEL_ENDPOINT takes precedence over
    OTEL_EXPORTER_OTLP_ENDPOINT, so it cannot be overridden by a stray OTel one."""
    return {
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_ENDPOINT": _collector_base(),
        "COPILOT_OTEL_CAPTURE_CONTENT": "false",
    }


DETAILED_VIEW = "OTEL_LOG_TOOL_DETAILS"


def _collector_is_local(base: str) -> bool:
    from urllib.parse import urlsplit
    return (urlsplit(base).hostname or "") in ("localhost", "127.0.0.1", "::1")


def detailed_view(state: str, target: Path, collector_confirmed: bool = False) -> int:
    """Switch Claude Code's detailed view on or off (ADR-014 decisions 12, 13).

    On only through the gate, enforced here rather than left to prose: the
    collector is local or the user vouches for it, and `stdtel-doctor
    --content-check` passes now, on this machine. The user's consent is the
    caller's to ask for — the stdtel-setup skill asks, then runs this.
    """
    current = json.loads(target.read_text() or "{}") if target.is_file() else {}
    env = current.setdefault("env", {})
    if state == "off":
        env.pop(DETAILED_VIEW, None)
        target.write_text(json.dumps(current, indent=2) + "\n")
        print(f"detailed view off in {target}. Restart Claude Code.")
        return 0

    from stdtel.doctor import claude_code_endpoint, probe_content
    base = claude_code_endpoint(target)       # where Claude Code will send content, not stdtel
    if not _collector_is_local(base) and not collector_confirmed:
        print(f"stdtel-install: the collector at {base} is not on this machine. The content check can "
              "only see Tempo and Loki here, not wherever that collector forwards. Re-run with "
              "--collector-confirmed only if you know it applies this repository's privacy rules.",
              file=sys.stderr)
        return 1
    check = probe_content(endpoint=base)
    if not check.ok:
        print(f"stdtel-install: not switching the detailed view on: {check.detail}. {check.remedy}",
              file=sys.stderr)
        return 1
    env[DETAILED_VIEW] = "1"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, indent=2) + "\n")
    print(f"detailed view on in {target} ({check.detail}). Restart Claude Code, then run "
          "`stdtel-doctor`, which re-checks this whenever the flag is on.")
    return 0


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
    p_set.add_argument("--replace-endpoint", action="store_true",
                       help="overwrite an OTEL_EXPORTER_OTLP_ENDPOINT already in the file (kept by default)")

    native = p_set.add_mutually_exclusive_group()
    native.add_argument("--no-native", action="store_true",
                        help="hooks only: do not switch on Claude Code's own telemetry (ADR-014)")
    native.add_argument("--native-only", action="store_true",
                        help="Claude Code's own telemetry only, no hooks: for a plugin install, where the "
                             "plugin registers the hooks and writing them here too would double-fire each")

    p_where = sub.add_parser("where", help="print the resolved absolute hook path")

    p_cop = sub.add_parser("copilot", help="print (or merge) Copilot's native OTel settings")
    p_cop.add_argument("--vscode-settings", type=Path,
                       help="merge into this VS Code settings.json (refused if it has comments)")
    p_dv = sub.add_parser("detailed-view",
                          help="switch Claude Code's OTEL_LOG_TOOL_DETAILS on (gated) or off")
    p_dv.add_argument("state", choices=["on", "off"])
    p_dv.add_argument("--path", type=Path, default=Path.home() / ".claude" / "settings.json")
    p_dv.add_argument("--collector-confirmed", action="store_true",
                      help="the collector is not local, and you know it applies this repo's privacy rules")
    p_load = sub.add_parser("loader", help="run stdtel-load on a schedule (launchd agent / systemd user timer)")
    p_load.add_argument("--uninstall", action="store_true", help="remove the schedule")
    p_load.add_argument("--interval", type=int, default=None,
                        help="seconds between runs (default STDTEL_LOAD_INTERVAL, else 900)")
    p_load.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)   # tests

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.cmd == "loader":
        return _loader(args)
    if args.cmd == "copilot":
        return _copilot(args.vscode_settings)
    if args.cmd == "settings" and args.native_only:
        return _native_only(args.path, args.dry_run, args.replace_endpoint)
    if args.cmd == "detailed-view":
        try:
            return detailed_view(args.state, args.path, args.collector_confirmed)
        except json.JSONDecodeError as e:
            print(f"stdtel-install: {args.path} is not valid JSON: {e}", file=sys.stderr)
            return 1

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
        kept = None
        if not args.no_native:
            block["env"], kept = _native_env_for(args.path, args.replace_endpoint)
        if args.dry_run:
            print(_dump(block))
        else:
            merged = merge_settings(args.path, block)
            print(f"wrote {len(block['hooks'])} hook events to {args.path} -> {binary}")
            if "env" in block:
                print(f"switched on Claude Code's own telemetry -> {merged['env']['OTEL_EXPORTER_OTLP_ENDPOINT']}")
                _report_kept(kept)
                if merged.get("env", {}).get("OTEL_METRICS_EXPORTER"):
                    print("note: OTEL_METRICS_EXPORTER is set in this file. stdtel leaves it alone; the "
                          "collector drops native metrics anyway (ADR-014 decision 4)")
            print(RESTART_NOTICE)
    return 0


def _loader(args) -> int:
    """ADR-015 decision 5: refuse rather than schedule a job that would fail every interval."""
    from stdtel import scheduler
    from stdtel.load import _extras_missing
    platform = scheduler.current_platform()
    if args.uninstall:
        return scheduler.uninstall(platform, args.home, run=scheduler._runner())
    missing = _extras_missing()
    if missing:
        print(f"stdtel-install: {', '.join(missing)} not installed beside stdtel; the scheduled loader "
              "would fail every run. Install the warehouse extras first: "
              "uv tool install 'stdtel[warehouse]'", file=sys.stderr)
        return 1
    try:
        interval = (args.interval if args.interval is not None
                    else int(os.environ.get("STDTEL_LOAD_INTERVAL") or 900))
        binary = scheduler.load_binary()
    except (ValueError, FileNotFoundError) as e:
        print(f"stdtel-install: {e}", file=sys.stderr)
        return 1
    if interval <= 0:
        print("stdtel-install: the interval must be a positive number of seconds", file=sys.stderr)
        return 1
    return scheduler.install(platform, args.home, binary, interval, run=scheduler._runner())


def _native_env_for(target: Path, replace_endpoint: bool) -> tuple[dict, str | None]:
    """The env block to merge, and the endpoint kept instead of replaced, if any.

    A different endpoint already in the file is the user's, and replacing it
    silently would redirect Claude Code's telemetry somewhere else — to localhost,
    when this runs in a shell without STDTEL_OTLP_ENDPOINT (#133). The protocol is
    kept with it: the two only work as a pair (4317 speaks gRPC).
    """
    env = claude_native_env()
    try:
        existing = (json.loads(target.read_text() or "{}").get("env") or {}) if target.is_file() else {}
    except (json.JSONDecodeError, AttributeError):
        return env, None                    # merge_settings reports the corrupt file
    old = str(existing.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").rstrip("/")
    if old and old != env["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/") and not replace_endpoint:
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT")
        env.pop("OTEL_EXPORTER_OTLP_PROTOCOL")
        return env, old
    return env, None


def _report_kept(kept: str | None) -> None:
    if kept:
        print(f"kept the existing OTEL_EXPORTER_OTLP_ENDPOINT ({kept}) and its protocol. "
              "Re-run with --replace-endpoint to point Claude Code at "
              f"{_collector_base()} instead")


def _native_only(target: Path, dry_run: bool, replace_endpoint: bool = False) -> int:
    env, kept = _native_env_for(target, replace_endpoint)
    block = {"env": env}
    if dry_run:
        print(_dump(block))
        return 0
    try:
        merged = merge_settings(target, block)
    except InstallError as e:
        print(f"stdtel-install: {e}", file=sys.stderr)
        return 1
    print(f"switched on Claude Code's own telemetry in {target} -> "
          f"{merged['env']['OTEL_EXPORTER_OTLP_ENDPOINT']} (hooks left to the plugin)")
    _report_kept(kept)
    if merged.get("env", {}).get("OTEL_METRICS_EXPORTER"):
        print("note: OTEL_METRICS_EXPORTER is set in this file. stdtel leaves it alone; the "
              "collector drops native metrics anyway (ADR-014 decision 4)")
    print("\nRestart Claude Code to pick this up.")
    return 0


def _copilot(vscode_settings: "Path | None") -> int:
    block = copilot_vscode_settings()
    if vscode_settings is not None:
        text = vscode_settings.read_text() if vscode_settings.is_file() else ""
        try:
            json.loads(text or "{}")
        except json.JSONDecodeError:
            print(f"stdtel-install: {vscode_settings} is not plain JSON (VS Code allows comments, and "
                  "rewriting it would drop them). Add these settings by hand:\n" + _dump(block),
                  file=sys.stderr)
            return 1
        current = json.loads(text or "{}")
        current.update(block)
        vscode_settings.parent.mkdir(parents=True, exist_ok=True)
        vscode_settings.write_text(json.dumps(current, indent=2) + "\n")
        print(f"wrote Copilot's OTel settings to {vscode_settings}; reload VS Code")
    else:
        print("VS Code settings.json:")
        print(_dump(block))
    print("\nCopilot CLI (shell profile):")
    for k, v in copilot_cli_env().items():
        print(f"export {k}={shlex.quote(v)}")
    print("\nOrganisations: the enterprise-managed `telemetry` policy is the preferred route; "
          "see docs/reference.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
