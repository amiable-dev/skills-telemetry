"""OTel span emission for std.skill.invocation. Metadata only — never content."""
from __future__ import annotations

import os
import time
from typing import Iterable

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SimpleSpanProcessor

SPAN_NAME = "std.skill.invocation"
DEFAULT_ENDPOINT = "http://localhost:4318"
DEFAULT_TIMEOUT_S = 2
FORBIDDEN_PREFIXES = ("gen_ai.input", "gen_ai.output", "gen_ai.prompt", "gen_ai.completion")


def build_provider(resource_attrs: dict, exporter: SpanExporter | None = None) -> TracerProvider:
    base = {"service.name": os.environ.get("OTEL_SERVICE_NAME", "stdtel"),
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


def emit_invocations(provider: TracerProvider, invocations: Iterable[dict], session_id: str) -> int:
    """Each invocation dict: started_at, ended_at, attributes(dict), error(bool)."""
    tracer = provider.get_tracer("stdtel", "0.1.0")
    n = 0
    for inv in invocations:
        start_ns = int(inv["started_at"] * 1e9)
        end_ns = int((inv.get("ended_at") or time.time()) * 1e9)
        attrs = scrub(inv.get("attributes", {}))
        attrs["session.id"] = session_id
        attrs["gen_ai.operation.name"] = "execute_tool"
        attrs["gen_ai.tool.name"] = "Skill"
        span = tracer.start_span(SPAN_NAME, attributes=attrs, start_time=start_ns)
        if inv.get("error"):
            span.set_status(trace.StatusCode.ERROR)
        span.end(end_time=end_ns)
        n += 1
    provider.force_flush()
    return n
