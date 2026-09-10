"""Incremental Claude Code transcript (JSONL) reader and token attribution.

Attribution rule (design §4.2/§6): tokens from llm requests *after* a skill
loaded and before the turn ends are the skill's "tail". When several skills
load in one turn the tail is split proportionally by load order (later skills
take the remainder after their own start), and a first-skill-only figure is
kept as a sensitivity check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: dict | "Usage") -> "Usage":
        src = other if isinstance(other, dict) else other.__dict__
        for k in TOKEN_KEYS:
            setattr(self, k, getattr(self, k) + int(src.get(k, 0) or 0))
        return self

    @property
    def total(self) -> int:
        return sum(getattr(self, k) for k in TOKEN_KEYS)

    def as_attributes(self) -> dict:
        return {f"gen_ai.usage.{k}": getattr(self, k) for k in TOKEN_KEYS}


@dataclass
class LlmRequest:
    ts: float
    model: str
    usage: Usage


@dataclass
class SkillLoad:
    ts: float
    skill: str
    tool_use_id: str | None
    load_tokens: int = 0
    caller: str = ""        # tool_use "caller.type", e.g. "direct"


@dataclass
class TranscriptSlice:
    requests: list[LlmRequest] = field(default_factory=list)
    skill_loads: list[SkillLoad] = field(default_factory=list)
    new_offset: int = 0


def _ts(entry: dict) -> float:
    t = entry.get("timestamp")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def read_slice(path: Path, offset: int = 0) -> TranscriptSlice:
    """Read new JSONL lines from byte offset. Tolerates partial trailing line."""
    out = TranscriptSlice(new_offset=offset)
    # Path("") is ".", which *exists* as a directory: exists() let it through and
    # open() then raised IsADirectoryError, which hooks swallow into a silent
    # no-telemetry state. Require an actual file.
    if not path.is_file():
        return out
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return out
    chunk, out.new_offset = data[: last_nl + 1], offset + last_nl + 1
    for raw in chunk.splitlines():
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and isinstance(msg, dict):
            usage = msg.get("usage")
            if usage:
                out.requests.append(LlmRequest(ts=_ts(e), model=msg.get("model", "unknown"),
                                               usage=Usage().add(usage)))
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    inp = block.get("input") or {}
                    caller = block.get("caller")
                    out.skill_loads.append(SkillLoad(
                        ts=_ts(e),
                        skill=str(inp.get("skill") or inp.get("name") or ""),
                        tool_use_id=block.get("id"),
                        caller=str((caller or {}).get("type") or "")))
        elif e.get("type") == "user" and isinstance(msg, dict):
            # tool_result for a Skill call: size of returned content approximates load tokens
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for sl in out.skill_loads:
                        if sl.tool_use_id and sl.tool_use_id == block.get("tool_use_id"):
                            content = block.get("content")
                            text = content if isinstance(content, str) else json.dumps(content or "")
                            sl.load_tokens = max(1, len(text) // 4)   # chars/4 heuristic
    return out


@dataclass
class Attribution:
    skill: str
    tail: Usage
    tail_first_only: Usage      # sensitivity check: all tokens after the *first* skill in the turn
    models: list[str]
    request_count: int


def attribute(sl: TranscriptSlice) -> list[Attribution]:
    """Assign post-load llm requests to skills in load order."""
    loads = sorted(sl.skill_loads, key=lambda s: s.ts)
    reqs = sorted(sl.requests, key=lambda r: r.ts)
    results: list[Attribution] = []
    for i, load in enumerate(loads):
        nxt = loads[i + 1].ts if i + 1 < len(loads) else float("inf")
        tail = Usage()
        models: list[str] = []
        n = 0
        for r in reqs:
            if load.ts <= r.ts < nxt:
                tail.add(r.usage); n += 1
                if r.model not in models:
                    models.append(r.model)
        first_only = Usage()
        if i == 0:
            for r in reqs:
                if r.ts >= load.ts:
                    first_only.add(r.usage)
        results.append(Attribution(skill=load.skill, tail=tail, tail_first_only=first_only,
                                   models=models, request_count=n))
    return results
