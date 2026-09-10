"""Replay of REAL hook payloads captured from a live Claude Code session.

Captured 2026-09-10 (Claude Code 2.1.267) by running `claude -p` headlessly with
capture hooks registered, then scrubbed of machine paths and free text. Every
other test in this suite asserts against payloads we invented; this one asserts
against what the harness actually sends, which is the only thing that settles
questions like "does `caller` reach the hook?" (it does not).
"""
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks
from stdtel.hooks.cli import _skill_from_payload

PAYLOADS = json.loads((Path(__file__).parent / "fixtures" / "hook_payloads.json").read_text())


def test_fixture_covers_the_lifecycle():
    assert set(PAYLOADS) == {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}


def test_skill_name_field_is_skill():
    """`skill`, not `name` and not `skill_name` — the latter is the OTel surface."""
    assert PAYLOADS["PreToolUse"]["tool_input"] == {"skill": "telemetry-probe"}
    assert _skill_from_payload(PAYLOADS["PreToolUse"])[0] == "telemetry-probe"


def test_caller_is_absent_from_the_hook_payload():
    """Why trigger falls back to the transcript: the harness does not send caller."""
    for event, payload in PAYLOADS.items():
        assert "caller" not in payload, event
    assert _skill_from_payload(PAYLOADS["PreToolUse"])[1] == "unknown"


def test_correlation_and_timing_fields_are_present():
    """These are what let us join to native claude_code.* telemetry."""
    assert PAYLOADS["PreToolUse"]["prompt_id"]
    assert PAYLOADS["PreToolUse"]["permission_mode"]
    assert PAYLOADS["PostToolUse"]["duration_ms"] > 0


def test_full_lifecycle_replay_produces_a_span(tmp_path):
    sid = PAYLOADS["PreToolUse"]["session_id"]
    hooks.session_start({**PAYLOADS["SessionStart"], "cwd": str(tmp_path)})
    hooks.pre_tool_use(PAYLOADS["PreToolUse"])
    hooks.post_tool_use(PAYLOADS["PostToolUse"])
    e = InMemorySpanExporter()
    assert hooks.stop({**PAYLOADS["Stop"], "transcript_path": ""}, exporter=e) == 1
    a = e.get_finished_spans()[0].attributes
    assert a["std.skill.name"] == "telemetry-probe"
    assert a["std.prompt.id"] == PAYLOADS["PreToolUse"]["prompt_id"]
    assert a["std.harness.permission_mode"] == "bypassPermissions"
    assert a["std.skill.duration_ms"] == PAYLOADS["PostToolUse"]["duration_ms"]


def test_stop_payload_has_no_transcript_landmine(tmp_path):
    """Stop carries transcript_path; the empty-string case is what killed us before."""
    assert "transcript_path" in PAYLOADS["Stop"]


def test_fixture_carries_no_content():
    """Metadata only: the capture deliberately drops tool_response and message text."""
    blob = json.dumps(PAYLOADS)
    for banned in ("last_assistant_message", "tool_response"):
        assert banned not in blob
