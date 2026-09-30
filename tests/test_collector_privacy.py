"""ADR-014 decision 3: the collector enforces privacy on every pipeline, for both
harnesses — tested against the real collector, not the YAML.

Verified live on 2026-09-30 (Claude Code 2.1.284): identity sits on every
metric data point and every event, not on the resource, so the resource-level
rule this project had would never have seen it. Content is deleted where it
lands, because both harnesses can send it (Claude Code with
OTEL_LOG_TOOL_DETAILS, Copilot despite captureContent:false — vscode#326254).

The pinned collector image runs this repository's config, with every pipeline's
exporter replaced by `debug` at detailed verbosity. Records shaped like each
harness's native output are sent over OTLP/HTTP, and the collector's own output
is read back. No bind mounts: config goes in with `docker cp`, and Docker picks
the port, so this runs the same on Colima (which shares only $HOME) and in CI.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

import pytest

from stdtel.enrich import branch_hash, repo_id
from tests.collector_harness import collector, docker_available

ROOT = Path(__file__).resolve().parent.parent

IDENTITY = ("user.email", "user.account_uuid", "user.account_id", "user.id", "organization.id")
CLAUDE_CONTENT = ("tool_input", "tool_parameters", "full_command", "bash_command", "error",
                  "prompt", "response")
COPILOT_CONTENT = ("github.copilot.tool.parameters.command", "github.copilot.tool.parameters.file_path",
                   "gen_ai.system_instructions", "gen_ai.tool.definitions",
                   "gen_ai.tool.call.arguments", "gen_ai.tool.call.result")
MARKER = "CONTENT-MARKER-" + uuid.uuid4().hex[:8]
VCS_TOKEN = "VCSTOKEN" + uuid.uuid4().hex[:8]
VCS_URL = f"https://x-access-token:{VCS_TOKEN}@github.com/amiable-dev/skills-telemetry.git"

#: (remote as Copilot reports it, branch). Every spelling must hash as the
#: capture side does, or a Copilot request never joins its change request.
REMOTES = [
    ("https://github.com/Amiable-Dev/Skills-Telemetry.git", "fix/media-hardening-233"),
    ("git@github.com:amiable-dev/skills-telemetry.git", "main"),
    ("https://x-access-token:SECRET@github.com/amiable-dev/skills-telemetry", "feature/PLAT-42-x"),
    ("https://gitlab.example.com:8443/group/sub/proj.git/", "release-0.7.0"),
]


pytestmark = pytest.mark.skipif(not docker_available(), reason="docker unavailable")


def _kv(attrs: dict) -> list:
    return [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]


def _identity() -> dict:
    return {k: f"{k}-value" for k in IDENTITY}


def _content() -> dict:
    return {k: MARKER for k in CLAUDE_CONTENT + COPILOT_CONTENT}


#: Both ways the collector is run. The Langfuse overlay redefines the traces
#: pipeline's processor list, so a privacy processor added only to the base
#: config would silently not run on the collector this machine actually uses.
OVERLAYS = ["overlay-none.yaml", "overlay-langfuse.yaml"]


@pytest.fixture(scope="module", params=OVERLAYS)
def collector_output(request):
    with collector(request.param) as c:
        ns = str(time.time_ns())
        spans = []
        for i, (remote, branch) in enumerate(REMOTES):
            spans.append({"traceId": f"{i + 1:032x}", "spanId": f"{i + 1:016x}", "name": "execute_tool",
                          "startTimeUnixNano": ns, "endTimeUnixNano": ns,
                          "attributes": _kv({**_identity(), **_content(),
                                             "github.copilot.git.repository": remote,
                                             "github.copilot.git.branch": branch,
                                             "github.copilot.git.commit_sha": "abc123",
                                             "stdtel.test.remote": remote})})
        c.post("/v1/traces", {"resourceSpans": [{"resource": {"attributes": _kv({"service.name": "copilot-chat"})},
                                                 "scopeSpans": [{"spans": spans}]}]})
        c.post("/v1/logs", {"resourceLogs": [{"resource": {"attributes": _kv({"service.name": "claude-code"})},
                                              "scopeLogs": [{"logRecords": [{
                                                  "timeUnixNano": ns,
                                                  "attributes": _kv({**_identity(), **_content(),
                                                                     "event.name": "api_request",
                                                                     "prompt.id": "p-1"})}]}]}]})
        c.post("/v1/metrics", {"resourceMetrics": [{"resource": {"attributes": _kv({"service.name": "claude-code"})},
                                                    "scopeMetrics": [{"metrics": [{
                                                        "name": "stdtel.privacy.probe",
                                                        "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                                                "dataPoints": [{"asInt": "5", "timeUnixNano": ns,
                                                                                "attributes": _kv({**_identity(),
                                                                                                   "type": "input"})}]}}]}]}]})
        # #128: Claude Code's repository URL, on the resource and on the record,
        # for every signal. Loki flattens both alike, so which one Claude Code
        # uses cannot be read back from storage.
        vcs = {"vcs.repository.url.full": VCS_URL}
        c.post("/v1/logs", {"resourceLogs": [{"resource": {"attributes": _kv({"service.name": "claude-code", **vcs})},
                                              "scopeLogs": [{"logRecords": [{
                                                  "timeUnixNano": ns,
                                                  "attributes": _kv({**vcs, "event.name": "vcs_probe"})}]}]}]})
        c.post("/v1/traces", {"resourceSpans": [{"resource": {"attributes": _kv({"service.name": "claude-code", **vcs})},
                                                 "scopeSpans": [{"spans": [{
                                                     "traceId": "f" * 32, "spanId": "f" * 16, "name": "vcs_probe_span",
                                                     "startTimeUnixNano": ns, "endTimeUnixNano": ns,
                                                     "attributes": _kv(vcs)}]}]}]})
        c.post("/v1/metrics", {"resourceMetrics": [{"resource": {"attributes": _kv({"service.name": "claude-code", **vcs})},
                                                    "scopeMetrics": [{"metrics": [{
                                                        "name": "stdtel.vcs.probe",
                                                        "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                                                "dataPoints": [{"asInt": "1", "timeUnixNano": ns,
                                                                                "attributes": _kv(vcs)}]}}]}]}]})
        yield c.wait_for("stdtel.privacy.probe", "prompt.id", "stdtel.test.remote", "vcs_probe",
                         "vcs_probe_span", "stdtel.vcs.probe")


def _keys(out: str) -> set[str]:
    return set(re.findall(r"-> ([\w.]+): ", out))


def test_the_collector_received_all_three_signals(collector_output):
    """Guards the rest against passing on an empty output."""
    assert "stdtel.privacy.probe" in collector_output, collector_output[-2000:]
    assert "prompt.id" in collector_output
    assert collector_output.count("stdtel.test.remote") >= len(REMOTES)


@pytest.mark.parametrize("key", CLAUDE_CONTENT + COPILOT_CONTENT)
def test_no_content_key_survives_any_pipeline(collector_output, key):
    assert key not in _keys(collector_output), f"{key} reached the exporter"


def test_the_content_marker_appears_nowhere(collector_output):
    assert MARKER not in collector_output


@pytest.mark.parametrize("key", IDENTITY)
def test_no_identity_survives_on_spans_events_or_data_points(collector_output, key):
    """Record-level, as verified live: a resource-level rule never sees these."""
    assert key not in _keys(collector_output), f"{key} reached the exporter"
    assert f"{key}-value" not in collector_output


def test_email_becomes_a_pseudonym_on_every_signal(collector_output):
    """The count of distinct people is the "5+ developers" half of the floor, so
    the pseudonym replaces the email rather than both disappearing."""
    assert collector_output.count("-> std.user.hash:") >= len(REMOTES) + 2


def test_copilots_branch_is_hashed_as_the_capture_side_hashes_it(collector_output):
    """ADR-013's join key for Copilot. If this ever differs from the capture
    side, Copilot requests stop joining their change requests, silently."""
    blocks = collector_output.split("Span #")[1:]
    seen = {}
    for block in blocks:
        remote = re.search(r"-> stdtel\.test\.remote: Str\(([^)]*)\)", block)
        got = re.search(r"-> std\.branch\.hash: Str\(([0-9a-f]+)\)", block)
        if remote:
            seen[remote.group(1)] = got.group(1) if got else None
    for remote, branch in REMOTES:
        assert seen.get(remote) == branch_hash(repo_id(remote), branch), (remote, seen.get(remote))


def test_the_plain_branch_and_remote_are_gone(collector_output):
    """A remote URL can carry a token (`x-access-token:SECRET@`), and a branch
    name can carry a customer's name."""
    keys = _keys(collector_output)
    assert "github.copilot.git.branch" not in keys and "github.copilot.git.repository" not in keys
    # the test's own label records the remote it sent; nothing else may carry it
    leaked = [line for line in collector_output.splitlines()
              if ("SECRET" in line or "fix/media-hardening-233" in line)
              and "stdtel.test.remote" not in line]
    assert not leaked, leaked


def test_the_commit_sha_is_kept(collector_output):
    """Opaque, and ADR-014 decision 8's evidence for Copilot."""
    assert "github.copilot.git.commit_sha" in _keys(collector_output)


def test_a_credential_in_claude_codes_repository_url_never_passes(collector_output):
    """#128: Claude Code sends `vcs.repository.url.full` on every event with
    OTEL_METRICS_INCLUDE_REPOSITORY, and a remote URL can carry a token."""
    assert "vcs_probe" in collector_output and "stdtel.vcs.probe" in collector_output
    assert VCS_TOKEN not in collector_output


def test_the_repository_url_is_kept_without_the_credential(collector_output):
    kept = re.findall(r"-> vcs\.repository\.url\.full: Str\(([^)]*)\)", collector_output)
    assert len(kept) >= 6, "resource and record, on three signals"
    assert set(kept) == {"https://github.com/amiable-dev/skills-telemetry.git"}
