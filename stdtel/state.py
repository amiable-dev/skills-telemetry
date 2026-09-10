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
    trigger: str            # auto | explicit | subagent
    started_at: float
    ended_at: float | None = None
    tool_use_id: str | None = None
    load_tokens: int = 0
    error: bool = False


@dataclass
class SessionState:
    session_id: str
    transcript_offset: int = 0
    resource: dict = field(default_factory=dict)   # std.ticket.id, std.repo, std.team, std.harness
    windows: list[SkillWindow] = field(default_factory=list)

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
                 resource=raw.get("resource", {}))
        st.windows = [SkillWindow(**w) for w in raw.get("windows", [])]
        return st

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "transcript_offset": self.transcript_offset,
            "resource": self.resource,
            "windows": [asdict(w) for w in self.windows],
        }, indent=1))

    def open_window(self, skill: str, version: str, trigger: str, tool_use_id: str | None) -> SkillWindow:
        w = SkillWindow(skill=skill, version=version, trigger=trigger,
                        started_at=time.time(), tool_use_id=tool_use_id)
        self.windows.append(w)
        return w

    def close_window(self, tool_use_id: str | None, error: bool = False) -> SkillWindow | None:
        for w in reversed(self.windows):
            if w.ended_at is None and (tool_use_id is None or w.tool_use_id == tool_use_id):
                w.ended_at = time.time()
                w.error = error
                return w
        return None

    def open_windows(self) -> list[SkillWindow]:
        return [w for w in self.windows if w.ended_at is None]

    def drain_closed(self) -> list[SkillWindow]:
        closed = [w for w in self.windows if w.ended_at is not None]
        self.windows = [w for w in self.windows if w.ended_at is None]
        return closed
