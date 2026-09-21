"""The capture contract: what an artefact activation is, and what each kind may carry.

ADR-009. One span name, `std.artefact.activation`, discriminated by
`std.artefact.kind`. The alternative — a span name per kind — was rejected
because the storage and query contract is what matters: one loader, one table,
one spanmetrics configuration, and "where did the tokens go, by artefact" as a
`GROUP BY kind`.

The cost of a single name is that a union schema validates weakly, so this
module *is* the validation. Every activation is built by a function here, and
`ALLOWED[kind]` is the exhaustive set of attribute keys that kind may carry. A
key outside it is dropped and reported, never emitted: an attribute that belongs
to another kind is a bug, and a `std.artefact.name` on a turn would put an
unbounded `prompt_id` into a metrics dimension.

Metadata only, as everywhere else. The builders take named scalars; no caller
ever hands this module a raw hook payload, which is what keeps a field like
`last_assistant_message` from having a path to a span in the first place.
"""
from __future__ import annotations

import sys

SPAN_NAME = "std.artefact.activation"
LEGACY_SKILL_SPAN_NAME = "std.skill.invocation"   # pre-ADR-009 rows still in Tempo
SESSION_SPAN_NAME = "std.session.cost"

KIND_SKILL = "skill"
KIND_SUBAGENT = "subagent"
KIND_COMPACTION = "compaction"
KIND_TURN = "turn"
KINDS = (KIND_SKILL, KIND_SUBAGENT, KIND_COMPACTION, KIND_TURN)

#: Set where a value was observed rather than reported by the harness. A
#: sub-agent read out of its transcript because no hook fired is not the same
#: measurement as one the harness handed us, and ADR-005 says so in the data.
SOURCE_HOOK = "hook"
SOURCE_TRANSCRIPT = "transcript"

_USAGE = frozenset({
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_creation_input_tokens",
})
#: ADR-010 containment. `name` is the scoping artefact and is stable for a whole
#: run; `key` is the unit instance (normally the ticket) and rolls every
#: iteration, so "what did each iteration cost" and "what did this run cost in
#: total" are the same rows grouped differently. `source` says whether the
#: declaration came from the artefact or was assumed on its behalf by an overlay
#: — an overlay is a local claim about someone else's artefact, and an analyst
#: has to be able to separate the two (ADR-011).
#:
#: `name`, `key` and `source` are bounded and may be metrics dimensions.
#: **`id` is not**: it is unique per container instance, which is #42 one layer
#: up, and the dashboard test forbids it in a PromQL expression.
SCOPE_NAME = "std.scope.name"
SCOPE_KEY = "std.scope.key"
SCOPE_ID = "std.scope.id"
SCOPE_SOURCE = "std.scope.source"
SCOPE_KEYS = frozenset({SCOPE_NAME, SCOPE_KEY, SCOPE_ID, SCOPE_SOURCE})
#: Where a scope declaration came from.
SCOPE_FROM_ARTEFACT = "artefact"
SCOPE_FROM_OVERLAY = "overlay"

_COMMON = (frozenset({"std.artefact.kind", "std.artefact.source", "session.id"})
           | _USAGE | SCOPE_KEYS)

#: Exhaustive per-kind attribute keys. Anything not listed is dropped.
#: `std.artefact.name` is deliberately absent from `turn`: a turn is identified
#: by `std.prompt.id`, which is unbounded, and `name` is a metrics dimension.
ALLOWED = {
    KIND_SKILL: _COMMON | {
        "std.artefact.name",
        "std.skill.name", "std.skill.invoked_as", "std.skill.version", "std.skill.plugin",
        "std.skill.trigger", "std.skill.load_tokens", "std.skill.tail_tokens",
        "std.skill.tail_tokens_first_only", "std.skill.llm_requests", "std.skill.duration_ms",
        "std.skill.content_hash", "std.skill.owner", "std.standard_id", "std.policy.ids",
        "std.prompt.id", "std.harness.permission_mode",
        "gen_ai.request.model", "gen_ai.operation.name", "gen_ai.tool.name",
    },
    KIND_SUBAGENT: _COMMON | {
        "std.artefact.name",
        "std.subagent.type", "std.subagent.id", "std.subagent.depth",
        "std.subagent.llm_requests", "std.subagent.tool_calls", "std.subagent.duration_ms",
        "std.artefact.parent_prompt_id",
        # ADR-011: what the agent's own file asserts, plus the hash of what it
        # actually says. Under std.agent.* rather than std.skill.* so a panel
        # grouping skills by version cannot silently gain agent rows.
        "std.agent.version", "std.agent.owner", "std.agent.content_hash",
        "std.standard_id", "std.policy.ids",
        "gen_ai.request.model", "gen_ai.operation.name",
    },
    KIND_COMPACTION: _COMMON | {
        "std.artefact.name",
        "std.compaction.reason", "std.compaction.tokens_before", "std.compaction.tokens_after",
        "std.compaction.turns_since_previous",
    },
    KIND_TURN: _COMMON | {
        "std.prompt.id", "std.turn.llm_requests", "std.turn.tool_calls",
        "std.turn.duration_ms", "std.turn.hook_ms", "std.harness.permission_mode",
        "gen_ai.request.model",
    },
}

#: Per-hook latency arrives as `std.turn.hook.<basename>.ms`, which cannot be
#: enumerated in advance. The basename is the only part kept — the payload
#: carries an absolute path, which is local filesystem layout, not telemetry.
_TURN_HOOK_PREFIX = "std.turn.hook."
_TURN_HOOK_SUFFIX = ".ms"


def allowed(kind: str, key: str) -> bool:
    if kind == KIND_TURN and key.startswith(_TURN_HOOK_PREFIX) and key.endswith(_TURN_HOOK_SUFFIX):
        return True
    return key in ALLOWED.get(kind, frozenset())


#: Command names that identify no hook. A hook invoked as `node ".../x.cjs" stop`
#: would otherwise be recorded as "node", and every scripted hook on the machine
#: would share one series.
_INTERPRETERS = frozenset({"node", "python", "python3", "sh", "bash", "zsh", "uv",
                           "uvx", "npx", "deno", "bun", "ruby", "perl", "env",
                           # subcommands of the above: `uv run x`, `python -m x`
                           "run", "exec", "tool", "-m", "-c", "--"})


def hook_latency_key(command: str) -> str:
    """`std.turn.hook.<basename>.ms` from a hook command line.

    The transcript records an absolute path, often behind an interpreter. A path
    says where this developer keeps their dotfiles; the basename says which hook
    was slow, which is the whole question.
    """
    import os
    tokens = [t.strip("\"'") for t in (command or "").split()]
    base = ""
    for token in tokens:
        candidate = os.path.basename(token)
        if not candidate or candidate.startswith("-"):
            continue
        if candidate.lower() in _INTERPRETERS and len(tokens) > 1:
            continue                      # keep looking for the script it runs
        base = candidate
        break
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base) or "unknown"
    return f"{_TURN_HOOK_PREFIX}{safe}{_TURN_HOOK_SUFFIX}"


def _clean(kind: str, attrs: dict) -> dict:
    """Drop keys this kind may not carry, and say so on stderr.

    Dropping rather than raising because hooks must not block the developer
    (ADR-003); reporting because a silent drop is the failure mode ADR-005
    exists to prevent. The contract test asserts our own builders never drop.
    """
    out, refused = {}, []
    for k, v in attrs.items():
        if v is None or v == "":
            continue                      # never record an unobserved value
        if not allowed(kind, k):
            refused.append(k)
            continue
        out[k] = v
    if refused:
        print(f"stdtel: dropped {len(refused)} attribute(s) not permitted on "
              f"kind={kind}: {', '.join(sorted(refused))}", file=sys.stderr)
    return out


def activation(kind: str, started_at: float, ended_at: float, attrs: dict,
               name: str | None = None, source: str = SOURCE_HOOK,
               error: bool = False) -> dict:
    """One activation, ready for the exporter or the spool."""
    if kind not in KINDS:
        raise ValueError(f"unknown artefact kind {kind!r}; expected one of {KINDS}")
    a = dict(attrs)
    a["std.artefact.kind"] = kind
    a["std.artefact.source"] = source
    if name:
        a["std.artefact.name"] = name
    return {"kind": kind, "started_at": started_at, "ended_at": ended_at,
            "attributes": _clean(kind, a), "error": error}


def subagent(agent_id: str, agent_type: str, started_at: float, ended_at: float,
             usage_attrs: dict, llm_requests: int, tool_calls: int, model: str = "",
             depth: int | None = None, parent_prompt_id: str = "",
             duration_ms: int | None = None, source: str = SOURCE_HOOK,
             manifest_attrs: dict | None = None) -> dict:
    """A sub-agent run.

    `agent_type` is the catalogue name of the agent (`Explore`, `general-purpose`,
    a plugin-scoped name); it is bounded, so it is the activation's name. The
    payload's `last_assistant_message` and `agent_transcript_path` are not
    parameters of this function, which is the point: there is no path by which
    either reaches a span.
    """
    attrs = {
        "std.subagent.id": agent_id,
        "std.subagent.type": agent_type,
        "std.subagent.llm_requests": llm_requests,
        "std.subagent.tool_calls": tool_calls,
        "std.artefact.parent_prompt_id": parent_prompt_id,
        "gen_ai.operation.name": "invoke_agent",
        **usage_attrs,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    if depth is not None:
        attrs["std.subagent.depth"] = depth
    if manifest_attrs:
        attrs.update(manifest_attrs)
    if duration_ms is not None:
        attrs["std.subagent.duration_ms"] = duration_ms
    return activation(KIND_SUBAGENT, started_at, ended_at, attrs,
                      name=agent_type or "unknown", source=source)


def compaction(reason: str, started_at: float, ended_at: float,
               tokens_before: int | None = None, tokens_after: int | None = None,
               turns_since_previous: int | None = None,
               source: str = SOURCE_HOOK) -> dict:
    """A context compaction.

    The token figures are the harness's own estimates and are recorded as
    received. Where the harness did not supply them they are omitted — a
    compaction with `tokens_before = 0` would read as one that dropped nothing.
    """
    attrs = {"std.compaction.reason": reason or "unknown"}
    if tokens_before is not None:
        attrs["std.compaction.tokens_before"] = tokens_before
    if tokens_after is not None:
        attrs["std.compaction.tokens_after"] = tokens_after
    if turns_since_previous is not None:
        attrs["std.compaction.turns_since_previous"] = turns_since_previous
    return activation(KIND_COMPACTION, started_at, ended_at, attrs,
                      name=reason or "unknown", source=source)


def turn(prompt_id: str, started_at: float, ended_at: float, usage_attrs: dict,
         llm_requests: int, tool_calls: int = 0, model: str = "",
         duration_ms: int | None = None, hook_ms: dict | None = None,
         permission_mode: str = "", source: str = SOURCE_TRANSCRIPT) -> dict:
    """One prompt-to-stop turn: the denominator for per-turn ratios.

    Not the denominator for skill effectiveness, which is the PR — see
    `../docs/evaluation-power.md`. A turn with no observed `prompt_id` is not an
    activation at all and callers must not build one.
    """
    if not prompt_id:
        raise ValueError("a turn activation requires an observed prompt_id")
    attrs = {
        "std.prompt.id": prompt_id,
        "std.turn.llm_requests": llm_requests,
        **usage_attrs,
    }
    if tool_calls:
        attrs["std.turn.tool_calls"] = tool_calls
    if model:
        attrs["gen_ai.request.model"] = model
    if duration_ms is not None:
        attrs["std.turn.duration_ms"] = duration_ms
    if permission_mode:
        attrs["std.harness.permission_mode"] = permission_mode
    for command, ms in sorted((hook_ms or {}).items()):
        attrs[hook_latency_key(command)] = ms
    if hook_ms:
        attrs["std.turn.hook_ms"] = sum(hook_ms.values())
    return activation(KIND_TURN, started_at, ended_at, attrs, source=source)


def stamp_scope(activations, scope: dict) -> None:
    """Add the open scope to every activation, in place.

    Applied after the activations are built rather than inside each builder,
    because the scope belongs to the window of time rather than to any one
    artefact — a sub-agent and a compaction that happen inside a loop iteration
    are as much part of its cost as the skill that opened it.

    **Everything in the window inherits the scope**, including work the scoping
    artefact did not cause. There is no observable causal link from a skill
    activation to a later tool call, and inventing one would record a
    relationship nobody measured (ADR-005). ADR-010 decision 9 is the other half
    of this: a rollup answers containment, never causation.
    """
    if not scope:
        return
    bad = set(scope) - SCOPE_KEYS
    if bad:                                   # a typo here would be dropped silently by _clean
        raise ValueError(f"not scope attributes: {sorted(bad)}")
    for inv in activations:
        inv.setdefault("attributes", {}).update(scope)
