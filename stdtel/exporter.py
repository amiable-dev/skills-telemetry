"""OTel span emission for std.artefact.activation. Metadata only — never content.

The span name and the per-kind attribute contract live in `stdtel.artefact`;
this module only puts them on the wire.
"""
from __future__ import annotations

import os
import time
from typing import Iterable

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SimpleSpanProcessor

from stdtel.artefact import (   # noqa: E402  (re-exported: importers use these names)
    KIND_TURN, LEGACY_SKILL_SPAN_NAME, SESSION_SPAN_NAME, SPAN_NAME,
)
DEFAULT_ENDPOINT = "http://localhost:4318"
DEFAULT_TIMEOUT_S = 2
# Widened when the hook began seeing every tool, not just Skill: tool_input and
# tool_response carry file contents, commands and diffs, none of which may leave
# the machine. Counts only.
FORBIDDEN_PREFIXES = ("gen_ai.input", "gen_ai.output", "gen_ai.prompt", "gen_ai.completion",
                      "tool.input", "tool.output", "tool.arguments", "tool.result",
                      "tool_input", "tool_response", "std.tool.input", "std.tool.output")


def build_provider(resource_attrs: dict, exporter: SpanExporter | None = None) -> TracerProvider:
    """Provider for one hook process.

    `service.instance.id` is pinned deliberately. Left unset the SDK generates a
    UUID per process, and prometheusremotewrite maps it to the `instance` label —
    so every hook opened a new time series, each counter reached 1 and stopped,
    and rate() was structurally zero (#42). Anything in resource_attrs still
    wins, for fleets that set their own.
    """
    from stdtel.identity import machine_id

    base = {"service.name": os.environ.get("OTEL_SERVICE_NAME", "stdtel"),
            "service.instance.id": machine_id(),
            "std.harness": resource_attrs.get("std.harness", "claude-code")}
    base.update({k: v for k, v in resource_attrs.items() if v is not None})
    provider = TracerProvider(resource=Resource.create(base))
    if exporter is None:
        provider.add_span_processor(BatchSpanProcessor(_otlp_exporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _endpoint() -> str:
    """Full traces endpoint URL.

    Claude Code strips `OTEL_*` from every subprocess it spawns, so a hook can
    never see OTEL_EXPORTER_OTLP_ENDPOINT no matter where it is set — it would
    silently fall back to localhost. STDTEL_OTLP_ENDPOINT survives the scrub and
    wins; the OTEL_* names stay as a fallback for direct CLI/CI use, where
    nothing scrubs them.
    """
    base = (os.environ.get("STDTEL_OTLP_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or DEFAULT_ENDPOINT).rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def _timeout() -> int:
    try:
        return max(1, int(os.environ.get("STDTEL_OTLP_TIMEOUT", DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _otlp_exporter():
    """OTLP/HTTP exporter that fails fast.

    The timeout must be set on the *exporter*: force_flush(timeout_millis=...)
    is ignored (open-telemetry/opentelemetry-python#4043), and the env var that
    would otherwise bound it (OTEL_EXPORTER_OTLP_TIMEOUT) is scrubbed before the
    hook ever runs. Unbounded, a dead collector stalls the Stop hook for ~7s of
    retry backoff on every turn.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=_endpoint(), timeout=_timeout())


def scrub(attrs: dict) -> dict:
    """Defence in depth: refuse to emit content attributes even if handed to us."""
    return {k: v for k, v in attrs.items()
            if not k.startswith(FORBIDDEN_PREFIXES) and isinstance(v, (str, int, float, bool))}


def emit_session_cost(provider: TracerProvider, attrs: dict, session_id: str,
                      started_at: float, ended_at: float) -> int:
    """One span per Stop carrying the session's whole token cost.

    Deliberately overlaps std.skill.invocation: that span attributes a slice of
    these tokens to a skill. The two must never be summed — session cost is the
    total, invocation tail is a share of it.
    """
    tracer = provider.get_tracer("stdtel", "0.1.0")
    a = scrub(dict(attrs))
    a["session.id"] = session_id
    span = tracer.start_span(SESSION_SPAN_NAME, attributes=a, start_time=int(started_at * 1e9))
    span.end(end_time=int(ended_at * 1e9))
    return 1


def _start(tracer, inv: dict, session_id: str, context=None):
    """One span, not yet ended, so a parent stays open while its children attach."""
    attrs = scrub(inv.get("attributes", {}))
    attrs["session.id"] = session_id
    span = tracer.start_span(SPAN_NAME, attributes=attrs,
                             start_time=int(inv["started_at"] * 1e9), context=context)
    if inv.get("error"):
        span.set_status(trace.StatusCode.ERROR)
    return span


def emit_activations(provider: TracerProvider, activations: Iterable[dict], session_id: str) -> int:
    """Each activation dict: kind, started_at, ended_at, attributes(dict), error(bool).

    Build them with `stdtel.artefact`, which applies the per-kind allowlist.
    `scrub()` runs again here as a second guard, never as the first one: nothing
    upstream hands this function a raw hook payload.

    **Activations are children of their turn** (ADR-010). Every span used to be
    started with no parent context, which makes it the root of its own trace — so
    containment was not merely unqueryable, it was unrecorded, while ADR-009 said
    the opposite (#75). The parent is the turn rather than the session because a
    session is resumed and persists: one real session spans seven weeks, and a
    trace that long outgrows what Tempo will hold, with late spans landing in
    fragments. A turn is emitted whole by one `Stop` process, so it is bounded.

    Turns are started first so that a child can attach to one. An activation whose
    turn is not in this batch stays a root rather than being invented a parent —
    that happens legitimately, as when a skill began before the last `Stop`.
    """
    tracer = provider.get_tracer("stdtel", "0.1.0")
    acts = list(activations)
    turns: dict[str, object] = {}
    open_spans: list[tuple] = []

    for inv in acts:
        if inv.get("kind") != KIND_TURN:
            continue
        span = _start(tracer, inv, session_id)
        prompt_id = (inv.get("attributes") or {}).get("std.prompt.id")
        if prompt_id:
            turns[prompt_id] = span
        open_spans.append((span, inv))

    for inv in acts:
        if inv.get("kind") == KIND_TURN:
            continue
        a = inv.get("attributes") or {}
        # A sub-agent names the turn that spawned it; a skill names the turn it
        # ran in. A compaction names neither, and stays a root: it happens
        # between turns, so claiming one would be inventing containment.
        parent = turns.get(a.get("std.prompt.id") or a.get("std.artefact.parent_prompt_id"))
        ctx = trace.set_span_in_context(parent) if parent is not None else None
        open_spans.append((_start(tracer, inv, session_id, context=ctx), inv))

    for span, inv in open_spans:
        span.end(end_time=int((inv.get("ended_at") or time.time()) * 1e9))
    provider.force_flush()
    return len(open_spans)
