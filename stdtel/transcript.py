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
class TurnMark:
    """Start of one turn. Verified against a real transcript: `promptId` is
    carried by `user` entries (1069 of 1069) and by no `assistant` entry, so the
    user entry is the only observable turn boundary."""
    ts: float
    prompt_id: str


@dataclass
class HookRun:
    """One hook invocation the harness timed for us (`system`/`stop_hook_summary`)."""
    ts: float
    command: str
    ms: int


@dataclass
class CompactionMark:
    """A `system`/`compact_boundary` entry. Compaction *appends* to the
    transcript rather than rewriting it — checked on a session with two
    compactions — so `transcript_offset` survives one and no turn is read
    twice."""
    ts: float
    reason: str
    tokens_before: int | None = None
    tokens_after: int | None = None


@dataclass
class TranscriptSlice:
    requests: list[LlmRequest] = field(default_factory=list)
    skill_loads: list[SkillLoad] = field(default_factory=list)
    turns: list[TurnMark] = field(default_factory=list)
    hook_runs: list[HookRun] = field(default_factory=list)
    compactions: list[CompactionMark] = field(default_factory=list)
    turn_durations: list[tuple] = field(default_factory=list)   # (ts, ms)
    #: (ts, tool_name) per tool_use block. Names and timings only — a tool's
    #: input and result are content and are never read here.
    tool_uses: list[tuple] = field(default_factory=list)
    #: Last `cost-state` entry seen. Cumulative for the whole session and
    #: carries no timestamp, so it is the session's total, never a turn's.
    cost_state: dict | None = None
    new_offset: int = 0

    def totals(self) -> Usage:
        """Every token in the slice, whether or not a skill was loaded.

        This is the denominator for cost-per-PR. Skill tail attribution only sees
        requests after a skill loads, so a session that never loads one is
        invisible to it — which is most sessions.
        """
        total = Usage()
        for r in self.requests:
            total.add(r.usage)
        return total

    def tool_counts(self) -> dict:
        counts: dict = {}
        for _ts_, name in self.tool_uses:
            counts[name] = counts.get(name, 0) + 1
        return counts

    def models(self) -> list[str]:
        seen = []
        for r in self.requests:
            if r.model not in seen:
                seen.append(r.model)
        return seen


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
    _seen_turns: set[str] = set()
    for raw in chunk.splitlines():
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = e.get("message") or {}
        etype = e.get("type")
        if etype == "system":
            # Three turn-level signals, all verified present in a real session:
            # turn_duration (88), stop_hook_summary (87), compact_boundary (2).
            sub = e.get("subtype")
            if sub == "turn_duration" and isinstance(e.get("durationMs"), int):
                out.turn_durations.append((_ts(e), e["durationMs"]))
            elif sub == "stop_hook_summary":
                for info in e.get("hookInfos") or []:
                    if isinstance(info, dict) and isinstance(info.get("durationMs"), int):
                        out.hook_runs.append(HookRun(ts=_ts(e), command=str(info.get("command") or ""),
                                                     ms=info["durationMs"]))
            elif sub == "compact_boundary":
                cm = e.get("compactMetadata") or {}
                out.compactions.append(CompactionMark(
                    ts=_ts(e), reason=str(cm.get("trigger") or "unknown"),
                    tokens_before=cm.get("preTokens"), tokens_after=cm.get("postTokens")))
            continue
        if etype == "cost-state":
            # Cumulative session totals, and the only place a real USD figure
            # appears. `cost_usd` has been NULL in the warehouse since the
            # schema was written because nothing read this.
            out.cost_state = {k: e.get(k) for k in
                              ("totalCostUSD", "totalDuration", "totalAPIDuration",
                               "totalToolDuration", "totalLinesAdded", "totalLinesRemoved")}
            continue
        if etype == "assistant" and isinstance(msg, dict):
            usage = msg.get("usage")
            if usage:
                out.requests.append(LlmRequest(ts=_ts(e), model=msg.get("model", "unknown"),
                                               usage=Usage().add(usage)))
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool = str(block.get("name") or "")
                if tool:
                    out.tool_uses.append((_ts(e), tool))
                if tool == "Skill":
                    inp = block.get("input") or {}
                    caller = block.get("caller")
                    out.skill_loads.append(SkillLoad(
                        ts=_ts(e),
                        skill=str(inp.get("skill") or inp.get("name") or ""),
                        tool_use_id=block.get("id"),
                        caller=str((caller or {}).get("type") or "")))
        elif etype == "user" and isinstance(msg, dict):
            # Every tool_result is also a `user` entry carrying the same
            # promptId — 1069 user entries against ~342 real turns in the
            # session this was checked on. Only the first marks the boundary.
            pid = e.get("promptId")
            if pid and not e.get("isMeta") and str(pid) not in _seen_turns:
                _seen_turns.add(str(pid))
                out.turns.append(TurnMark(ts=_ts(e), prompt_id=str(pid)))
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


@dataclass
class TurnAttribution:
    """One prompt-to-stop turn, with what it spent.

    A turn is the denominator for per-turn ratios and the join key to the
    harness's native `claude_code.*` metrics. It is *not* the denominator for
    skill effectiveness, which is the PR (`docs/evaluation-power.md`).
    """
    prompt_id: str
    started_at: float
    ended_at: float
    usage: Usage
    models: list[str]
    request_count: int
    tool_calls: int = 0
    duration_ms: int | None = None
    hook_ms: dict = field(default_factory=dict)   # {command: total ms}


def attribute_turns(sl: TranscriptSlice, open_turn: str = "",
                    open_turn_started_at: float = 0.0,
                    now: float | None = None) -> list[TurnAttribution]:
    """Split a slice into turns on the observed `promptId` boundaries.

    A slice usually begins mid-turn, because Stop fires inside the turn that is
    ending. Requests before the first boundary belong to `open_turn`, the
    prompt_id carried in session state from the previous Stop — an observation
    made earlier, not a guess. Without it those tokens would be dropped or,
    worse, folded into the next turn.
    """
    marks = sorted(sl.turns, key=lambda t: t.ts)
    windows: list[tuple[str, float, float]] = []
    end_of_slice = now if now is not None else max(
        [r.ts for r in sl.requests] + [m.ts for m in marks] + [0.0])
    if open_turn:
        first_boundary = marks[0].ts if marks else end_of_slice
        windows.append((open_turn, open_turn_started_at or 0.0, first_boundary))
    for i, m in enumerate(marks):
        nxt = marks[i + 1].ts if i + 1 < len(marks) else end_of_slice
        windows.append((m.prompt_id, m.ts, nxt))

    out: list[TurnAttribution] = []
    for pid, start, end in windows:
        usage, models, n = Usage(), [], 0
        for r in sl.requests:
            if start <= r.ts < end or (end == start and r.ts == start):
                usage.add(r.usage)
                n += 1
                if r.model not in models:
                    models.append(r.model)
        tools = sum(1 for ts, _name in sl.tool_uses if start <= ts < end)
        duration = next((ms for ts, ms in sl.turn_durations if start <= ts < end), None)
        hooks: dict = {}
        for h in sl.hook_runs:
            if start <= h.ts < end and h.command:
                hooks[h.command] = hooks.get(h.command, 0) + h.ms
        out.append(TurnAttribution(prompt_id=pid, started_at=start, ended_at=end,
                                   usage=usage, models=models, request_count=n,
                                   tool_calls=tools, duration_ms=duration, hook_ms=hooks))
    return out


@dataclass
class SubagentSummary:
    """What a sub-agent run cost, read from its own transcript.

    Only `usage` blocks, tool *names* and timestamps are read. Message bodies
    are never touched, and neither is the `description` in the sibling
    `.meta.json`, which is a task instruction written by whoever spawned the
    agent — content by any reading.
    """
    usage: Usage
    models: list[str]
    request_count: int
    tool_calls: int
    started_at: float
    ended_at: float


def summarise_subagent(path: Path) -> SubagentSummary | None:
    """Sum one sub-agent transcript. None when it is absent or empty.

    `usage` blocks here are per request, not cumulative — checked across three
    real sub-agent transcripts, where output_tokens moves both up and down
    between consecutive requests. Summing them is therefore correct and cannot
    double-count.
    """
    sl = read_slice(path, 0)
    if not sl.requests and not sl.tool_uses:
        return None
    stamps = [r.ts for r in sl.requests if r.ts] + [ts for ts, _ in sl.tool_uses if ts]
    return SubagentSummary(
        usage=sl.totals(), models=sl.models(), request_count=len(sl.requests),
        tool_calls=len(sl.tool_uses),
        started_at=min(stamps) if stamps else 0.0,
        ended_at=max(stamps) if stamps else 0.0)
