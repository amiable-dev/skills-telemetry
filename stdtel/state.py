"""Per-session state shared between hooks (start/stop of skill invocations)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


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

    @property
    def path(self) -> Path:
        return state_dir() / f"{self.session_id}.json"

    @classmethod
    def load(cls, session_id: str) -> "SessionState":
        p = state_dir() / f"{session_id}.json"
        if not p.exists():
            return cls(session_id=session_id)
        raw = json.loads(p.read_text())
        st = cls(session_id=session_id, transcript_offset=raw.get("transcript_offset", 0),
                 started_at=raw.get("started_at", 0.0), resource=raw.get("resource", {}),
                 last_export_ok=raw.get("last_export_ok"))
        st.windows = [SkillWindow(**w) for w in raw.get("windows", [])]
        st.tool_calls = {k: list(v) for k, v in (raw.get("tool_calls") or {}).items()}
        return st

    def save(self) -> None:
        """Every field must be listed here.

        Each hook is a separate process, so anything not written is lost between
        events — silently, because the field simply reads as its default. Two
        fields were added without being persisted and produced plausible-looking
        zeros rather than an error.
        """
        self.path.write_text(json.dumps({
            "transcript_offset": self.transcript_offset,
            "started_at": self.started_at,
            "resource": self.resource,
            "windows": [asdict(w) for w in self.windows],
            "tool_calls": self.tool_calls,
            "last_export_ok": self.last_export_ok,
        }, indent=1))

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

    def open_windows(self) -> list[SkillWindow]:
        return [w for w in self.windows if w.ended_at is None]

    def drain_closed(self) -> list[SkillWindow]:
        closed = [w for w in self.windows if w.ended_at is not None]
        self.windows = [w for w in self.windows if w.ended_at is None]
        return closed
