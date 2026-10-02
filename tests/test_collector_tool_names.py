"""#142: names survive the collector; content does not.

With the detailed view on, Claude Code puts the MCP server and tool names, the
Skill tool's `skill_name` and the Agent tool's `subagent_type` only inside
`tool_parameters` — the same JSON that carries `bash_command`. The tool name
itself is the literal "mcp_tool" for every user-configured server (Claude Code
monitoring docs, read 2026-10-02). Deleting `tool_parameters` wholesale threw the
names away with the content, so the opt-in bought nothing that reached storage.

The collector now lifts an allowlist of name keys out first. Tested on the
pinned image, under both overlays, with a content marker beside the names.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from tests.collector_harness import collector, docker_available, kv

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker unavailable")

MARKER = "CONTENT-" + uuid.uuid4().hex[:10]
NAMES = {"mcp_server_name": "llm-council", "mcp_tool_name": "verify",
         "skill_name": "epic-loop", "subagent_type": "Explore"}


def _params(**extra) -> str:
    return json.dumps({**extra, "bash_command": MARKER, "full_command": MARKER, "description": MARKER,
                       "git_branch": MARKER})


@pytest.fixture(scope="module", params=["overlay-none.yaml", "overlay-langfuse.yaml"])
def out(request):
    ns = str(time.time_ns())
    records = [
        {"event.name": "tool_result", "tool_name": "mcp_tool",
         "tool_parameters": _params(mcp_server_name="llm-council", mcp_tool_name="verify"), "probe": "mcp"},
        {"event.name": "tool_result", "tool_name": "Skill", "tool_parameters": _params(skill_name="epic-loop"),
         "probe": "skill"},
        {"event.name": "tool_decision", "tool_name": "Agent", "tool_parameters": _params(subagent_type="Explore"),
         "probe": "agent"},
        {"event.name": "tool_result", "tool_name": "Bash", "tool_parameters": "not json " + MARKER, "probe": "bad"},
        {"event.name": "tool_result", "tool_name": "mcp_tool",
         "tool_parameters": json.dumps({"mcp_server_name": "x" * 300 + MARKER}), "probe": "long"},
    ]
    with collector(request.param) as c:
        c.post("/v1/logs", {"resourceLogs": [{"resource": {"attributes": kv({"service.name": "claude-code"})},
                                              "scopeLogs": [{"logRecords": [
                                                  {"timeUnixNano": ns, "attributes": kv(r)} for r in records]}]}]})
        c.post("/v1/traces", {"resourceSpans": [{"resource": {"attributes": kv({"service.name": "claude-code"})},
                                                 "scopeSpans": [{"spans": [{
                                                     "traceId": "e" * 32, "spanId": "e" * 16, "name": "claude_code.tool",
                                                     "startTimeUnixNano": ns, "endTimeUnixNano": ns,
                                                     "attributes": kv({**records[0], "probe": "span"})}]}]}]})
        yield c.wait_for("-> probe: Str(long)", "-> probe: Str(span)")


def _record(out: str, probe: str) -> str:
    """The debug exporter's block for one probe record."""
    import re
    for block in re.split(r"(?:LogRecord|Span) #\d+", out)[1:]:   # one record per block, never two
        if f"-> probe: Str({probe})" in block:
            return block
    raise AssertionError(f"probe {probe} never reached the exporter")


@pytest.mark.parametrize("probe,key", [("mcp", "mcp_server_name"), ("mcp", "mcp_tool_name"),
                                       ("skill", "skill_name"), ("agent", "subagent_type"),
                                       ("span", "mcp_server_name")])
def test_names_are_lifted_out_before_the_content_goes(out, probe, key):
    assert f"-> {key}: Str({NAMES[key]})" in _record(out, probe)


def test_the_content_beside_them_is_still_dropped(out):
    assert MARKER not in out
    assert "-> tool_parameters:" not in out


def test_only_the_allowlisted_keys_are_lifted(out):
    for probe in ("mcp", "skill", "agent", "span"):
        block = _record(out, probe)
        for key in ("bash_command", "full_command", "description", "git_branch"):
            assert f"-> {key}:" not in block


def test_a_parameters_string_that_is_not_json_lifts_nothing(out):
    block = _record(out, "bad")
    assert not any(f"-> {k}:" in block for k in NAMES)


def test_a_name_that_is_not_name_shaped_is_not_lifted(out):
    """A name is short and plain. Anything else could be content wearing a name's key."""
    assert "-> mcp_server_name:" not in _record(out, "long")
