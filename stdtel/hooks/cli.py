"""Single entrypoint for Claude Code hooks: `stdtel-hook <event>`.

Reads the hook JSON payload from stdin. Events: session-start, pre-tool-use,
post-tool-use, stop. Always exits 0 so a telemetry failure never blocks the
developer (design §7: memory is best-effort, telemetry likewise).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from stdtel.enrich import resource_attributes
from stdtel.exporter import build_provider, emit_invocations
from stdtel.manifest import load_catalogue
from stdtel.state import SessionState
from stdtel.transcript import attribute, read_slice


def _payload() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _catalogue():
    root = Path(os.environ.get("STDTEL_SKILLS_ROOT", "skills"))
    try:
        return load_catalogue(root)
    except Exception:
        return {}


def _skill_from_input(inp: dict) -> tuple[str, str]:
    name = str(inp.get("skill") or inp.get("name") or "")
    trigger = "explicit" if name.startswith("/") or inp.get("explicit") else "auto"
    return name.lstrip("/"), trigger


def session_start(p: dict) -> None:
    st = SessionState.load(p.get("session_id", "unknown"))
    st.resource = resource_attributes(Path(p.get("cwd", ".")))
    st.save()


def pre_tool_use(p: dict) -> None:
    if p.get("tool_name") != "Skill":
        return
    st = SessionState.load(p.get("session_id", "unknown"))
    name, trigger = _skill_from_input(p.get("tool_input") or {})
    cat = _catalogue()
    version = cat[name].version if name in cat else "unversioned"
    st.open_window(name, version, trigger, p.get("tool_use_id"))
    st.save()


def post_tool_use(p: dict, error: bool = False) -> None:
    if p.get("tool_name") != "Skill":
        return
    st = SessionState.load(p.get("session_id", "unknown"))
    st.close_window(p.get("tool_use_id"), error=error)
    st.save()


def stop(p: dict, exporter=None) -> int:
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
        attrs = {
            "std.skill.name": w.skill,
            "std.skill.version": w.version,
            "std.skill.trigger": w.trigger,
            "std.skill.load_tokens": loads[w.skill].load_tokens if w.skill in loads else 0,
        }
        if w.skill in cat:
            attrs.update(cat[w.skill].as_attributes())
        a = attributions.get(w.skill)
        if a:
            attrs["std.skill.tail_tokens"] = a.tail.total
            attrs["std.skill.tail_tokens_first_only"] = a.tail_first_only.total
            attrs["std.skill.llm_requests"] = a.request_count
            attrs["gen_ai.request.model"] = a.models[0] if a.models else "unknown"
            attrs.update(a.tail.as_attributes())
        invocations.append({"started_at": w.started_at, "ended_at": w.ended_at,
                            "attributes": attrs, "error": w.error})
    st.save()
    if not invocations:
        return 0
    provider = build_provider(st.resource, exporter=exporter)
    return emit_invocations(provider, invocations, sid)


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
    except Exception as e:   # never block the developer
        print(f"stdtel: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
