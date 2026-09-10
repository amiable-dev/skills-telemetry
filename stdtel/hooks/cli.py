"""Single entrypoint for Claude Code hooks: `stdtel-hook <event>`.

Reads the hook JSON payload from stdin. Events: session-start, pre-tool-use,
post-tool-use, stop. Always exits 0 so a telemetry failure never blocks the
developer (design §7: memory is best-effort, telemetry likewise).

Every stdtel import is deliberately function-local. PreToolUse and PostToolUse
fire on *every* Skill call and are pure latency in the developer's loop, so they
must not pay for OpenTelemetry (~19ms) or PyYAML when they never touch them.
Only `stop` needs the exporter; only the catalogue lookup needs the parser.
Module scope stays stdlib-only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def skills_roots() -> list[Path]:
    """Directories to scan for SKILL.md, in precedence order.

    STDTEL_SKILLS_ROOT may name several roots separated by os.pathsep; a relative
    one is resolved against CLAUDE_PROJECT_DIR (the project the hook fired in),
    not the process cwd, so `skills` in a project settings file keeps working.
    The user-level catalogue is always searched last, which is what makes hooks
    registered once in ~/.claude/settings.json useful from every project.
    """
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    roots = []
    for raw in os.environ.get("STDTEL_SKILLS_ROOT", "").split(os.pathsep):
        if raw.strip():
            root = Path(raw).expanduser()
            roots.append(root if root.is_absolute() else base / root)
    roots.append(Path.home() / ".claude" / "skills")
    out, seen = [], set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            out.append(root)
    return out


def _catalogue():
    """Merged catalogue across roots; earlier roots win on name collisions."""
    from stdtel.manifest import load_catalogue          # pulls in PyYAML

    cat: dict = {}
    for root in skills_roots():
        try:
            found = load_catalogue(root, strict=False)
        except Exception:
            continue
        for name, manifest in found.items():
            cat.setdefault(name, manifest)
    return cat


def _skill_from_payload(p: dict) -> tuple[str, str]:
    """(skill name as invoked, trigger).

    Field name verified against 120 real Skill invocations across 114 transcripts
    in ~/.claude/projects: the input carries `skill` every time (never `name`,
    never `skill_name` — that is the OTel event surface, a different payload).
    `args` also appears and is deliberately not captured: it can hold content.

    Trigger is provisional here. The transcript carries the tool_use `caller`
    block, which is ground truth, so Stop upgrades this value; a name is never
    slash-prefixed in the observed data, so nothing sets "explicit" in practice.
    """
    inp = p.get("tool_input") or p.get("toolArgs") or {}
    name = str(inp.get("skill") or inp.get("name") or "")
    caller = p.get("caller")
    if name.startswith("/"):
        trigger = "explicit"
    elif isinstance(caller, dict) and caller.get("type"):
        trigger = str(caller["type"])
    else:
        trigger = "unknown"
    return name.lstrip("/"), trigger


def _resolve(name: str, cat: dict):
    """(manifest | None, catalogue name) for a skill as invoked.

    Plugin-provided skills arrive namespaced — `epic-loop:epic-loop`,
    `anthropic-skills:skill-creator` — which is 71% of real invocations, and is
    what every skill looks like once distributed as a plugin. The catalogue is
    keyed on the bare front-matter `name`, so match the full string first, then
    the segment after the last ":".
    """
    if name in cat:
        return cat[name], name
    bare = name.rsplit(":", 1)[-1]
    if bare in cat:
        return cat[bare], bare
    return None, bare


def session_start(p: dict) -> None:
    from stdtel.enrich import resource_attributes
    from stdtel.state import SessionState

    st = SessionState.load(p.get("session_id", "unknown"))
    st.resource = resource_attributes(Path(p.get("cwd", ".")), payload=p)
    st.started_at = st.started_at or time.time()
    st.save()


def pre_tool_use(p: dict) -> None:
    if p.get("tool_name") != "Skill":
        return
    from stdtel.state import SessionState

    st = SessionState.load(p.get("session_id", "unknown"))
    name, trigger = _skill_from_payload(p)
    # Version is resolved from the catalogue at Stop, which loads it anyway to
    # attach the rest of the manifest attributes. Reading it here too would put
    # a PyYAML parse of every SKILL.md on the hot path for a value Stop discards.
    st.open_window(name, "unversioned", trigger, p.get("tool_use_id"),
                   prompt_id=str(p.get("prompt_id") or ""),
                   permission_mode=str(p.get("permission_mode") or ""))
    st.save()


def post_tool_use(p: dict, error: bool = False) -> None:
    """Fires for every tool, not just Skill.

    Tool-call failure rate is a first-class metric and a leading indicator of a
    skill instructing the model to do something the environment cannot do. The
    non-Skill path stays at the interpreter floor: state only, no catalogue, no
    exporter, and counts only — tool_input and tool_response never leave here.
    """
    from stdtel.state import SessionState

    tool = str(p.get("tool_name") or p.get("toolName") or "")
    st = SessionState.load(p.get("session_id", "unknown"))
    st.record_tool(tool, failed=error)
    if tool != "Skill":
        st.save()
        return
    w = st.close_window(p.get("tool_use_id"), error=error)
    if w is not None and p.get("duration_ms"):
        w.duration_ms = int(p["duration_ms"])      # the harness times the tool call itself
    st.save()


def stop(p: dict, exporter=None) -> int:
    from stdtel.exporter import build_provider, emit_invocations, emit_session_cost
    from stdtel.state import SessionState
    from stdtel.transcript import attribute, read_slice

    sid = p.get("session_id", "unknown")
    st = SessionState.load(sid)
    transcript = Path(p.get("transcript_path", ""))
    sl = read_slice(transcript, st.transcript_offset)
    st.transcript_offset = sl.new_offset
    attributions = {a.skill: a for a in attribute(sl)}
    loads = {l.skill: l for l in sl.skill_loads}
    cat = _catalogue()
    # any window still open at Stop is closed now (turn ended)
    for w in st.open_windows():
        st.close_window(w.tool_use_id)
    invocations = []
    for w in st.drain_closed():
        manifest, resolved = _resolve(w.skill, cat)
        load = loads.get(w.skill)
        attrs = {
            "std.skill.name": resolved,
            "std.skill.invoked_as": w.skill,
            "std.skill.version": w.version,
            # the transcript's caller block is ground truth; the hook payload may not carry it
            "std.skill.trigger": (load.caller if load and load.caller else w.trigger),
            "std.skill.load_tokens": load.load_tokens if load else 0,
        }
        if ":" in w.skill:
            attrs["std.skill.plugin"] = w.skill.rsplit(":", 1)[0]
        if w.prompt_id:
            # join key to Claude Code's native claude_code.* telemetry
            attrs["std.prompt.id"] = w.prompt_id
        if w.permission_mode:
            attrs["std.harness.permission_mode"] = w.permission_mode
        if w.duration_ms:
            attrs["std.skill.duration_ms"] = w.duration_ms
        if manifest:
            attrs.update(manifest.as_attributes())
        a = attributions.get(w.skill)
        if a:
            attrs["std.skill.tail_tokens"] = a.tail.total
            attrs["std.skill.tail_tokens_first_only"] = a.tail_first_only.total
            attrs["std.skill.llm_requests"] = a.request_count
            attrs["gen_ai.request.model"] = a.models[0] if a.models else "unknown"
            attrs.update(a.tail.as_attributes())
        invocations.append({"started_at": w.started_at, "ended_at": w.ended_at,
                            "attributes": attrs, "error": w.error})
    # The session's whole cost, emitted whether or not a skill was ever loaded.
    # Without this a session that used no skill produces no telemetry at all, and
    # cost-per-PR has no denominator (CLAUDE.md: unattributed sessions are kept
    # for cost analysis, excluded from outcome analysis).
    totals = sl.totals()
    tool_calls, tool_failures = st.tool_totals()
    session_attrs = {}
    if sl.requests or tool_calls:
        session_attrs = {
            "std.session.llm_requests": len(sl.requests),
            "gen_ai.request.model": (sl.models() or ["unknown"])[0],
            "std.session.tool_calls": tool_calls,
            "std.session.tool_failures": tool_failures,
            **totals.as_attributes(),
        }
        # per-tool counts as std.session.tool.<name>.{calls,failures}
        for name, (calls, failures) in sorted(st.tool_calls.items()):
            session_attrs[f"std.session.tool.{name}.calls"] = calls
            if failures:
                session_attrs[f"std.session.tool.{name}.failures"] = failures
        st.tool_calls = {}          # drained with the windows
    st.save()
    if not invocations and not session_attrs:
        return 0
    provider = build_provider(st.resource, exporter=exporter)
    emitted = emit_invocations(provider, invocations, sid) if invocations else 0
    if session_attrs:
        # Session start comes from state, not the transcript: transcript timestamps
        # can be absent or unparseable, and a start near the epoch turns the span's
        # duration into "seconds since 1970" rather than the session's length.
        now = time.time()
        started = st.started_at or min((i["started_at"] for i in invocations), default=now)
        emitted += emit_session_cost(provider, session_attrs, sid, started, now)
    return emitted


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    p = _payload()
    try:
        if event == "session-start":
            session_start(p)
        elif event == "pre-tool-use":
            pre_tool_use(p)
        elif event == "post-tool-use":
            post_tool_use(p)
        elif event == "post-tool-use-failure":
            post_tool_use(p, error=True)
        elif event == "stop":
            stop(p)
        else:
            # still exit 0: an unknown event must never block the developer
            print(f"stdtel-hook: unknown event {event!r}; expected one of "
                  f"session-start, pre-tool-use, post-tool-use, "
                  f"post-tool-use-failure, stop", file=sys.stderr)
    except Exception as e:   # never block the developer
        print(f"stdtel: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
