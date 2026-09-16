"""Per-session state shared between hooks (start/stop of skill invocations)."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@contextlib.contextmanager
def locked(session_id: str):
    """Hold the session's lock for a read-modify-write.

    Hooks are separate processes and several can run at once — a sub-agent's
    start and stop fire while the parent is still calling tools — so two of them
    read the same state, each adds its own change, and the later write silently
    discards the earlier one. Worse, the writes can interleave in the file
    itself: a shorter document landing inside a longer one leaves valid JSON
    followed by a fragment, which `load()` then refuses for the rest of the
    session (#73).

    flock is advisory and process-local, which is exactly the scope needed: every
    writer is a hook on this machine. A lock that cannot be taken is not worth
    failing a turn over, so the timeout falls through to an unlocked write —
    still atomic, just last-write-wins.
    """
    path = state_dir() / f"{session_id}.lock"
    handle = None
    try:
        handle = path.open("a+")
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception:                             # noqa: BLE001 - no fcntl, or no lock
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()


def state_dir() -> Path:
    d = Path(os.environ.get("STDTEL_STATE_DIR", Path.home() / ".stdtel" / "sessions"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class SkillWindow:
    skill: str
    version: str
    trigger: str            # caller.type from the transcript, or "unknown"
    started_at: float
    ended_at: float | None = None
    tool_use_id: str | None = None
    load_tokens: int = 0
    error: bool = False
    # straight from the hook payload (validated against a live session 2026-09-10)
    prompt_id: str = ""         # correlates with native claude_code.* telemetry
    permission_mode: str = ""   # measured harness mode, not the env's guess
    duration_ms: int = 0        # the harness's own timing, better than our clock


@dataclass
class SubagentWindow:
    """One sub-agent run, opened by SubagentStart and closed by SubagentStop.

    `transcript_path` is kept so Stop can sum the run's tokens. It stays in
    local state and is never an attribute on a span: a path is this machine's
    filesystem layout, and ADR-009 keeps it off the wire along with
    `last_assistant_message`.
    """
    agent_id: str
    agent_type: str
    started_at: float
    ended_at: float | None = None
    transcript_path: str = ""
    parent_prompt_id: str = ""
    depth: int = 0


@dataclass
class CompactionEvent:
    """A PostCompact observation. Token estimates are omitted when the harness
    did not supply them — a compaction recorded as dropping 0 tokens would be a
    fabricated measurement (ADR-005)."""
    reason: str
    at: float
    tokens_before: int | None = None
    tokens_after: int | None = None
    turns_since_previous: int | None = None


@dataclass
class SessionState:
    session_id: str
    transcript_offset: int = 0
    started_at: float = 0.0        # wall clock at SessionStart; see stop()
    resource: dict = field(default_factory=dict)   # std.ticket.id, std.repo, std.team, std.harness
    windows: list[SkillWindow] = field(default_factory=list)
    # {tool_name: [calls, failures]} — counts only, never inputs or results.
    # Aggregated per session rather than one span per tool call: at ~30 calls per
    # prompt, per-call spans would multiply telemetry volume for a metric that
    # only needs counts.
    tool_calls: dict = field(default_factory=dict)
    # outcome of the last export, so the statusline can say "spans are dropping"
    # from local state rather than probing the collector on every render
    last_export_ok: bool | None = None
    # ADR-009 artefacts. Sub-agents and compactions arrive on their own hook
    # events, which fire between Stops, so they queue here until Stop exports.
    subagents: list = field(default_factory=list)
    compactions: list = field(default_factory=list)
    # The turn in flight. Stop fires *inside* a turn, so the next slice opens
    # mid-turn; carrying the prompt_id forward attributes those tokens to the
    # turn they belong to instead of dropping them.
    open_prompt_id: str = ""
    open_prompt_started_at: float = 0.0
    # Sub-agents already exported by the transcript fallback, so a directory
    # scan at the next Stop does not emit them a second time. Unused when the
    # SubagentStop hook is firing, which is the observed path.
    seen_agent_ids: list = field(default_factory=list)
    turn_count: int = 0
    turns_at_last_compaction: int = 0
    # Which ADR-009 hook events this machine has actually seen fire. The doctor
    # reports "not yet observed" rather than implying capture works because the
    # settings file mentions it.
    observed_events: list = field(default_factory=list)

    @property
    def path(self) -> Path:
        return state_dir() / f"{self.session_id}.json"

    @classmethod
    def mutate(cls, session_id: str):
        """Load, let the caller change it, save — all under the session lock.

        Use this for every hook that adds to state. Loading and saving
        separately is what loses a sub-agent whose window was written while
        another hook held an older copy.
        """
        @contextlib.contextmanager
        def _ctx():
            with locked(session_id):
                st = cls.load(session_id)
                yield st
                st.save(lock=False)
        return _ctx()

    @classmethod
    def load(cls, session_id: str) -> "SessionState":
        p = state_dir() / f"{session_id}.json"
        if not p.exists():
            return cls(session_id=session_id)
        raw = cls._read(p, session_id)
        if raw is None:
            return cls(session_id=session_id)
        st = cls(session_id=session_id, transcript_offset=raw.get("transcript_offset", 0),
                 started_at=raw.get("started_at", 0.0), resource=raw.get("resource", {}),
                 last_export_ok=raw.get("last_export_ok"))
        st.windows = [SkillWindow(**w) for w in raw.get("windows", [])]
        st.tool_calls = {k: list(v) for k, v in (raw.get("tool_calls") or {}).items()}
        st.subagents = [SubagentWindow(**s) for s in raw.get("subagents", [])]
        st.compactions = [CompactionEvent(**c) for c in raw.get("compactions", [])]
        st.open_prompt_id = raw.get("open_prompt_id", "")
        st.open_prompt_started_at = raw.get("open_prompt_started_at", 0.0)
        st.seen_agent_ids = list(raw.get("seen_agent_ids") or [])
        st.turn_count = raw.get("turn_count", 0)
        st.turns_at_last_compaction = raw.get("turns_at_last_compaction", 0)
        st.observed_events = list(raw.get("observed_events") or [])
        return st

    @staticmethod
    def _read(path: Path, session_id: str) -> dict | None:
        """Parse the state file, salvaging what a torn write left behind.

        Before this, a corrupt file raised out of `load()` and the hook's
        catch-all swallowed it — so telemetry for that session stopped dead and
        exited 0 for the rest of its life. Found on a real session: 42 spans,
        then silence for a day, with a valid document followed by 235 bytes of a
        longer one. Repairing it by hand made the next Stop emit 47 spans.

        A torn write leaves the *whole* of one document followed by the tail of
        another, so the leading document is intact and is a real, slightly older
        state. Taking it keeps `transcript_offset`, which starting fresh would
        reset — re-reading the whole transcript and re-emitting turns already
        recorded.
        """
        text = path.read_text()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            salvaged, _end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            print(f"stdtel: {path.name} is unreadable and could not be salvaged; "
                  f"starting this session's state over. Spans already sent are unaffected; "
                  f"the transcript will be re-read from the beginning.", file=sys.stderr)
            return None
        print(f"stdtel: {path.name} was torn by concurrent hooks and has been repaired from "
              f"the intact leading document. Any state written after it is lost.", file=sys.stderr)
        return salvaged if isinstance(salvaged, dict) else None

    def save(self, lock: bool = True) -> None:
        """Every field must be listed here.

        Each hook is a separate process, so anything not written is lost between
        events — silently, because the field simply reads as its default. Two
        fields were added without being persisted and produced plausible-looking
        zeros rather than an error.
        """
        payload = json.dumps({
            "transcript_offset": self.transcript_offset,
            "started_at": self.started_at,
            "resource": self.resource,
            "windows": [asdict(w) for w in self.windows],
            "tool_calls": self.tool_calls,
            "last_export_ok": self.last_export_ok,
            "subagents": [asdict(s) for s in self.subagents],
            "compactions": [asdict(c) for c in self.compactions],
            "open_prompt_id": self.open_prompt_id,
            "open_prompt_started_at": self.open_prompt_started_at,
            "seen_agent_ids": self.seen_agent_ids[-200:],
            "turn_count": self.turn_count,
            "turns_at_last_compaction": self.turns_at_last_compaction,
            "observed_events": self.observed_events,
        }, indent=1)
        if lock:
            with locked(self.session_id):
                self._write(payload)
        else:
            self._write(payload)

    def _write(self, payload: str) -> None:
        """Replace the file in one step.

        `write_text` truncates and then writes, so a reader — or a second writer
        — can see a half-written file, and two writers can interleave into one
        that parses as valid JSON followed by a fragment. Writing a temporary
        file beside it and renaming is atomic on POSIX: a reader sees either the
        old file or the new one, never a blend of both.
        """
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{self.session_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def open_window(self, skill: str, version: str, trigger: str, tool_use_id: str | None,
                    prompt_id: str = "", permission_mode: str = "") -> SkillWindow:
        w = SkillWindow(skill=skill, version=version, trigger=trigger,
                        started_at=time.time(), tool_use_id=tool_use_id,
                        prompt_id=prompt_id, permission_mode=permission_mode)
        self.windows.append(w)
        return w

    def close_window(self, tool_use_id: str | None, error: bool = False) -> SkillWindow | None:
        for w in reversed(self.windows):
            if w.ended_at is None and (tool_use_id is None or w.tool_use_id == tool_use_id):
                w.ended_at = time.time()
                w.error = error
                return w
        return None

    def record_tool(self, tool_name: str, failed: bool = False) -> None:
        if not tool_name:
            return
        entry = self.tool_calls.setdefault(tool_name, [0, 0])
        entry[0] += 1
        if failed:
            entry[1] += 1

    def tool_totals(self) -> tuple[int, int]:
        calls = sum(v[0] for v in self.tool_calls.values())
        failures = sum(v[1] for v in self.tool_calls.values())
        return calls, failures

    def observe(self, event: str) -> None:
        """Record that a hook event really fired on this machine."""
        if event and event not in self.observed_events:
            self.observed_events.append(event)

    def open_subagent(self, agent_id: str, agent_type: str, transcript_path: str = "",
                      parent_prompt_id: str = "", depth: int = 0) -> SubagentWindow:
        w = SubagentWindow(agent_id=agent_id, agent_type=agent_type, started_at=time.time(),
                           transcript_path=transcript_path, parent_prompt_id=parent_prompt_id,
                           depth=depth)
        self.subagents.append(w)
        return w

    def close_subagent(self, agent_id: str, agent_type: str = "", transcript_path: str = "",
                       parent_prompt_id: str = "") -> SubagentWindow:
        """Close the open window for this agent, or open and close one now.

        SubagentStart may not have fired — it is a newer event than SubagentStop
        on some harness versions, and a sub-agent can outlive the session that
        spawned it. A window created here has `started_at == ended_at`, so its
        duration is not reported rather than reported as zero.
        """
        for w in reversed(self.subagents):
            if w.ended_at is None and w.agent_id == agent_id:
                w.ended_at = time.time()
                w.agent_type = w.agent_type or agent_type
                w.transcript_path = transcript_path or w.transcript_path
                w.parent_prompt_id = w.parent_prompt_id or parent_prompt_id
                return w
        now = time.time()
        w = SubagentWindow(agent_id=agent_id, agent_type=agent_type, started_at=now,
                           ended_at=now, transcript_path=transcript_path,
                           parent_prompt_id=parent_prompt_id)
        self.subagents.append(w)
        return w

    def drain_subagents(self) -> list[SubagentWindow]:
        done = [s for s in self.subagents if s.ended_at is not None]
        self.subagents = [s for s in self.subagents if s.ended_at is None]
        return done

    def record_compaction(self, reason: str, tokens_before: int | None = None,
                          tokens_after: int | None = None) -> CompactionEvent:
        since = self.turn_count - self.turns_at_last_compaction if self.turn_count else None
        self.turns_at_last_compaction = self.turn_count
        c = CompactionEvent(reason=reason or "unknown", at=time.time(),
                            tokens_before=tokens_before, tokens_after=tokens_after,
                            turns_since_previous=since)
        self.compactions.append(c)
        return c

    def drain_compactions(self) -> list[CompactionEvent]:
        out, self.compactions = list(self.compactions), []
        return out

    def open_windows(self) -> list[SkillWindow]:
        return [w for w in self.windows if w.ended_at is None]

    def drain_closed(self) -> list[SkillWindow]:
        closed = [w for w in self.windows if w.ended_at is not None]
        self.windows = [w for w in self.windows if w.ended_at is None]
        return closed
