"""Single entrypoint for Claude Code hooks: `stdtel-hook <event>`.

Reads the hook JSON payload from stdin. Events: session-start, pre-tool-use,
post-tool-use, subagent-start, subagent-stop, post-compact, stop. Always exits 0 so a telemetry failure never blocks the
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


def _roots_from(var: str, extra: list[Path] | None = None) -> list[Path]:
    """Existing directories named by an os.pathsep list, in order, deduplicated.

    A relative entry resolves against CLAUDE_PROJECT_DIR (the project the hook
    fired in), not the process cwd, so `skills` in a project settings file keeps
    working.
    """
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
    roots = []
    for raw in os.environ.get(var, "").split(os.pathsep):
        if raw.strip():
            root = Path(raw).expanduser()
            roots.append(root if root.is_absolute() else base / root)
    roots.extend(extra or [])
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


def skills_roots() -> list[Path]:
    """Directories to scan for SKILL.md, in precedence order.

    The user-level catalogue is always searched last, which is what makes hooks
    registered once in ~/.claude/settings.json useful from every project.
    """
    return _roots_from("STDTEL_SKILLS_ROOT", [Path.home() / ".claude" / "skills"])


def overlay_roots() -> list[Path]:
    """Directories of contract stubs for skills we do not own (ADR-004, #51).

    Editing somebody else's SKILL.md to onboard it works exactly once: the next
    upstream release overwrites it, a plugin reinstall replaces the directory,
    and a new machine has none of it — all silently. An overlay lives somewhere
    the operator controls and fills in what those skills do not state.
    """
    return _roots_from("STDTEL_SKILLS_OVERLAY")


def _catalogue():
    """Merged catalogue across roots, then overlays applied to fill gaps.

    Roots are winner-takes-all: the earliest one wins a name collision. Overlays
    are the opposite and deliberately so — they supply only what a skill does not
    state itself. An overlay that overrode would keep asserting its pinned value
    after upstream started declaring a real one, and nothing would notice (#51).
    """
    from stdtel.manifest import fill_gaps, load_catalogue, load_overlay   # pulls in PyYAML

    cat: dict = {}
    for root in skills_roots():
        try:
            found = load_catalogue(root, strict=False)
        except Exception:
            continue
        for name, manifest in found.items():
            cat.setdefault(name, manifest)
    for root in overlay_roots():
        try:
            stubs = load_overlay(root)
        except Exception:
            continue
        for name, stub in stubs.items():
            cat[name] = fill_gaps(cat[name], stub) if name in cat else stub
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
    import stdtel
    from stdtel.enrich import resource_attributes
    from stdtel.plugin import skew_notice
    from stdtel.state import SessionState

    st = SessionState.load(p.get("session_id", "unknown"))
    st.resource = resource_attributes(Path(p.get("cwd", ".")), payload=p)
    st.started_at = st.started_at or time.time()
    st.save()
    # Once per session, on the only event whose output the developer sees. The
    # plugin and the package are separate installs and neither updates the
    # other, so drift is normal and silent: the hooks a stale plugin registers
    # still work, while the skills it ships quietly lag the release.
    try:
        notice = skew_notice(stdtel.__version__)
    except Exception:                    # noqa: BLE001 - never block the developer
        notice = ""
    if notice:
        print(notice)


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


def subagent_start(p: dict) -> None:
    """A sub-agent was spawned. Records only when it began and what it is.

    Nothing is exported here: the run's cost is only knowable once it ends, and
    a hook that exported per sub-agent would put a socket in the spawn path.
    """
    from stdtel.state import SessionState

    agent_id = str(p.get("agent_id") or "")
    if not agent_id:
        return
    # Under the lock: sub-agents start and stop while the parent is still
    # calling tools, so two hooks read the same state and the later write
    # discards the earlier one — or interleaves into a torn file (#73).
    with SessionState.mutate(p.get("session_id", "unknown")) as st:
        st.observe("subagent-start")
        st.open_subagent(agent_id=agent_id, agent_type=str(p.get("agent_type") or ""),
                         transcript_path=str(p.get("agent_transcript_path") or ""),
                         parent_prompt_id=str(p.get("prompt_id") or ""))


def subagent_stop(p: dict) -> None:
    """A sub-agent finished.

    Five named scalars are taken from the payload. `last_assistant_message` is
    the sub-agent's final reply and `agent_transcript_path` is this machine's
    filesystem layout; neither is read into an attribute, and the raw payload is
    never stored, logged or serialised anywhere (ADR-009).
    """
    from stdtel.state import SessionState

    agent_id = str(p.get("agent_id") or "")
    if not agent_id:
        return
    with SessionState.mutate(p.get("session_id", "unknown")) as st:
        st.observe("subagent-stop")
        st.close_subagent(agent_id=agent_id, agent_type=str(p.get("agent_type") or ""),
                          transcript_path=str(p.get("agent_transcript_path") or ""),
                          parent_prompt_id=str(p.get("prompt_id") or ""))


def post_compact(p: dict) -> None:
    """Context was compacted. Queued for the next Stop to export.

    The token figures are the harness's own estimates and reach the span
    unchanged. Where a harness does not send them they are omitted: a
    compaction recorded as dropping zero tokens is a fabricated measurement.
    """
    from stdtel.state import SessionState

    before = p.get("token_count_estimate_before")
    after = p.get("token_count_estimate_after")
    with SessionState.mutate(p.get("session_id", "unknown")) as st:
        st.observe("post-compact")
        st.record_compaction(reason=str(p.get("compaction_reason") or "unknown"),
                             tokens_before=before if isinstance(before, int) else None,
                             tokens_after=after if isinstance(after, int) else None)


def _subagent_activations(st, sl, transcript: Path) -> list:
    """Activations for every sub-agent that finished since the last Stop.

    Two sources, and the span says which. The SubagentStop hook is an
    observation and is preferred. Where that hook has never fired on this
    machine — an older harness, or a settings file that predates it — the
    session's `subagents/` directory is scanned instead, which is an inference
    and is flagged `source=transcript` so no query can confuse the two.
    """
    from stdtel import artefact
    from stdtel.transcript import summarise_subagent

    out = []
    for w in st.drain_subagents():
        path = Path(w.transcript_path) if w.transcript_path else _subagent_path(transcript, w.agent_id)
        summary = summarise_subagent(path) if path else None
        if summary is None:
            continue
        duration = int((w.ended_at - w.started_at) * 1000) if w.ended_at and w.ended_at > w.started_at else None
        out.append(artefact.subagent(
            agent_id=w.agent_id, agent_type=w.agent_type or "unknown",
            started_at=summary.started_at or w.started_at,
            ended_at=summary.ended_at or (w.ended_at or w.started_at),
            usage_attrs=summary.usage.as_attributes(), llm_requests=summary.request_count,
            tool_calls=summary.tool_calls, model=(summary.models or [""])[0],
            depth=w.depth or None, parent_prompt_id=w.parent_prompt_id,
            duration_ms=duration, source=artefact.SOURCE_HOOK))
    if "subagent-stop" in st.observed_events:
        return out
    for path in _subagent_transcripts(transcript):
        agent_id = path.stem.replace("agent-", "")
        if agent_id in st.seen_agent_ids:
            continue
        summary = summarise_subagent(path)
        if summary is None:
            continue
        st.seen_agent_ids.append(agent_id)
        out.append(artefact.subagent(
            agent_id=agent_id, agent_type=_subagent_type(path) or "unknown",
            started_at=summary.started_at, ended_at=summary.ended_at,
            usage_attrs=summary.usage.as_attributes(), llm_requests=summary.request_count,
            tool_calls=summary.tool_calls, model=(summary.models or [""])[0],
            source=artefact.SOURCE_TRANSCRIPT))
    return out


def _subagent_dir(transcript: Path) -> Path | None:
    """`<session dir>/subagents`, next to the session transcript.

    Verified against real sessions: each run is `agent-<agent_id>.jsonl`, and
    the id in the file matches the one in the name for every file checked.
    """
    if not transcript or not transcript.name.endswith(".jsonl"):
        return None
    d = transcript.parent / transcript.stem / "subagents"
    return d if d.is_dir() else None


def _subagent_path(transcript: Path, agent_id: str) -> Path | None:
    d = _subagent_dir(transcript)
    if d is None or not agent_id:
        return None
    path = d / f"agent-{agent_id}.jsonl"
    return path if path.is_file() else None


def _subagent_transcripts(transcript: Path) -> list:
    d = _subagent_dir(transcript)
    return sorted(d.glob("agent-*.jsonl")) if d else []


def _subagent_type(path: Path) -> str:
    """`agentType` from the sibling meta file, and nothing else from it.

    That file also carries `description`, the task the agent was given, which is
    a prompt in everything but name.
    """
    meta = path.with_suffix("").with_name(path.stem + ".meta.json")
    try:
        return str(json.loads(meta.read_text()).get("agentType") or "")
    except Exception:
        return ""


def _compaction_activations(st, sl) -> list:
    """Compactions, from the PostCompact hook or, failing that, the transcript."""
    from stdtel import artefact

    out = []
    for c in st.drain_compactions():
        out.append(artefact.compaction(
            reason=c.reason, started_at=c.at, ended_at=c.at,
            tokens_before=c.tokens_before, tokens_after=c.tokens_after,
            turns_since_previous=c.turns_since_previous, source=artefact.SOURCE_HOOK))
    if "post-compact" in st.observed_events:
        return out
    for m in sl.compactions:
        out.append(artefact.compaction(
            reason=m.reason, started_at=m.ts, ended_at=m.ts,
            tokens_before=m.tokens_before, tokens_after=m.tokens_after,
            source=artefact.SOURCE_TRANSCRIPT))
    return out


def _turn_activations(st, sl, permission_mode: str, now: float) -> list:
    """One activation per turn observed in this slice.

    Each carries the slice's *delta*, not a running total, so the rare case of
    two Stops inside one turn adds a second row rather than overwriting the
    first or losing its tokens. Anything counting turns therefore counts
    `DISTINCT std.prompt.id`, which `warehouse/efficiency/` does.
    """
    from stdtel import artefact
    from stdtel.transcript import attribute_turns

    out = []
    turns = attribute_turns(sl, open_turn=st.open_prompt_id,
                            open_turn_started_at=st.open_prompt_started_at, now=now)
    for t in turns:
        if not t.prompt_id or (t.request_count == 0 and not t.tool_calls):
            continue
        out.append(artefact.turn(
            prompt_id=t.prompt_id, started_at=t.started_at or now, ended_at=t.ended_at or now,
            usage_attrs=t.usage.as_attributes(), llm_requests=t.request_count,
            tool_calls=t.tool_calls, model=(t.models or [""])[0],
            duration_ms=t.duration_ms, hook_ms=t.hook_ms, permission_mode=permission_mode))
    if sl.turns:
        st.turn_count += len(sl.turns)
        st.open_prompt_id = sl.turns[-1].prompt_id
        st.open_prompt_started_at = sl.turns[-1].ts
    return out


def _refresh_ticket(st, cwd: Path) -> None:
    """Re-derive `std.ticket.id` from the branch, every Stop (#77).

    It used to be computed once in `session_start` and reused for the life of the
    session. `std.ticket.id` is the join key for all delivery data (ADR-002), so a
    stale one does not fail — it joins cleanly to the *wrong* pull request, and
    `scorecard.sql` then attributes one ticket's cost and policy outcome to
    another. A confident wrong answer, which ADR-005 treats as worse than a gap,
    and unrecoverable afterwards because the branch at the moment of the span is
    gone.

    Per Stop is the granularity, not per span: every span in one Stop shares the
    branch as it is now. That is the turn, which is the unit a developer changes
    branches between, and it is what ADR-010 scopes containment to anyway.

    An unreadable branch leaves the previous value untouched. Overwriting a good
    ticket with `unattributed` because git happened not to answer would turn a
    transient failure into permanent data loss.
    """
    from stdtel.enrich import current_branch, ticket_from_branch

    branch = current_branch(cwd)
    if branch:
        st.resource["std.ticket.id"] = ticket_from_branch(branch)


def stop(p: dict, exporter=None) -> int:
    from stdtel import artefact
    from stdtel.exporter import build_provider, emit_activations, emit_session_cost
    from stdtel.state import SessionState
    from stdtel.transcript import attribute, read_slice

    sid = p.get("session_id", "unknown")
    st = SessionState.load(sid)
    _refresh_ticket(st, Path(p.get("cwd", ".")))
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
        if manifest is not None and not manifest.telemetry_emit:
            # `telemetry.emit: false` in SKILL.md. Parsed and validated since the
            # first commit, and until now read by nothing — an advertised control
            # that did nothing. The session total still counts these tokens; what
            # is suppressed is attributing them to this skill by name.
            continue
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
        attrs["gen_ai.operation.name"] = "execute_tool"
        attrs["gen_ai.tool.name"] = "Skill"
        invocations.append(artefact.activation(
            artefact.KIND_SKILL, w.started_at, w.ended_at or time.time(),
            attrs, name=resolved, error=w.error))
    now = time.time()
    invocations.extend(_turn_activations(st, sl, str(p.get("permission_mode") or ""), now))
    invocations.extend(_subagent_activations(st, sl, transcript))
    invocations.extend(_compaction_activations(st, sl))
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
    # Real money, from the harness's own `cost-state` entry. Cumulative for the
    # session and carrying no timestamp, so it belongs to the session span and
    # never to a turn. `session_cost.cost_usd` has been NULL since the schema
    # was written because nothing read this.
    if sl.cost_state:
        cost = sl.cost_state.get("totalCostUSD")
        if isinstance(cost, (int, float)):
            session_attrs["std.session.cost_usd"] = float(cost)
        for src, dst in (("totalDuration", "std.session.duration_ms"),
                         ("totalAPIDuration", "std.session.api_ms"),
                         ("totalToolDuration", "std.session.tool_ms")):
            if isinstance(sl.cost_state.get(src), int):
                session_attrs[dst] = sl.cost_state[src]
    st.save()
    if not invocations and not session_attrs:
        return 0
    # ADR-008: when spooling, the hook writes to disk and opens no socket at all.
    # stdtel-export drains it. A hook that never talks to the network cannot stall
    # on one, which is what "never block the developer" was reaching for.
    if exporter is None and _spooling():
        from stdtel.exporter import SESSION_SPAN_NAME, SPAN_NAME
        from stdtel.spool import append
        rows = [{"name": SPAN_NAME, "session_id": sid, "resource": st.resource,
                 "kind": i.get("kind"),
                 "started_at": i["started_at"], "ended_at": i["ended_at"],
                 "attributes": i["attributes"], "error": i.get("error", False)}
                for i in invocations]
        if session_attrs:
            rows.append({"name": SESSION_SPAN_NAME, "session_id": sid, "resource": st.resource,
                         "started_at": (st.started_at or time.time()), "ended_at": time.time(),
                         "attributes": session_attrs})
        st.last_export_ok = True          # spooled successfully; export is someone else's job
        st.save()
        return append(rows)

    provider = build_provider(st.resource, exporter=exporter)
    try:
        emitted = emit_activations(provider, invocations, sid) if invocations else 0
        st.last_export_ok = True
    except Exception:
        st.last_export_ok = False
        st.save()
        raise
    if session_attrs:
        # Session start comes from state, not the transcript: transcript timestamps
        # can be absent or unparseable, and a start near the epoch turns the span's
        # duration into "seconds since 1970" rather than the session's length.
        started = st.started_at or min((i["started_at"] for i in invocations), default=now)
        emitted += emit_session_cost(provider, session_attrs, sid, started, now)
    st.save()
    return emitted


def _spooling() -> bool:
    """Write spans to disk rather than exporting inline (ADR-008)."""
    return os.environ.get("STDTEL_SPOOL", "").strip().lower() in ("1", "true", "yes", "on")


def disabled() -> bool:
    """True when the developer has switched telemetry off.

    Checked before the payload is even read: an opt-out that still parses your
    transcript is not an opt-out.
    """
    return os.environ.get("STDTEL_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else ""
    if disabled():
        return 0
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
        elif event == "subagent-start":
            subagent_start(p)
        elif event == "subagent-stop":
            subagent_stop(p)
        elif event == "post-compact":
            post_compact(p)
        elif event == "stop":
            stop(p)
        else:
            # still exit 0: an unknown event must never block the developer
            print(f"stdtel-hook: unknown event {event!r}; expected one of "
                  f"session-start, pre-tool-use, post-tool-use, "
                  f"post-tool-use-failure, subagent-start, subagent-stop, "
                  f"post-compact, stop", file=sys.stderr)
    except Exception as e:   # never block the developer
        print(f"stdtel: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
