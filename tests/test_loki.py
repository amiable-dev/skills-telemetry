"""ADR-014 decision 4: events get a store, and native metrics do not go to
Prometheus (#110).

Claude Code's per-request records — cost, tokens, skill.name, prompt.id — exist
only as events, and the logs pipeline exported only to `debug`, so every one
would have been discarded. Native metrics carry `session.id` as a label on every
data point (verified live), which would open a series per session; deleting the
label instead recreates #92. The per-request events carry everything the metrics
do, so the metrics are dropped at the collector and the warehouse is the one
analytic path for native records.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest
import yaml

from tests.collector_harness import collector, docker_available, kv, metric

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
COLLECTOR = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())
LOKI_HOST_PORT = 11010          # berth: skills-telemetry extra `loki`, claimed 2026-09-30


# --- the stack ---------------------------------------------------------------------------

def test_loki_is_in_the_stack_pinned_and_on_loopback():
    svc = COMPOSE["services"]["loki"]
    assert re.match(r"^grafana/loki:\d+\.\d+\.\d+$", svc["image"]), "pin an exact version"
    assert svc["ports"] == [f"${{STDTEL_BIND:-127.0.0.1}}:{LOKI_HOST_PORT}:3100"]
    assert any("loki.yaml" in v for v in svc["volumes"])
    assert "otel-collector" in COMPOSE["services"] and "loki" in COMPOSE["services"]["otel-collector"].get("depends_on", [])


def test_grafana_can_read_loki_by_a_pinned_uid():
    ds = yaml.safe_load((ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "ds.yaml").read_text())
    loki = [d for d in ds["datasources"] if d["type"] == "loki"]
    assert loki and loki[0]["uid"] == "Loki" and loki[0]["url"] == "http://loki:3100"


def test_the_logs_pipeline_stores_events_in_loki():
    """`debug` alone discarded every per-request record."""
    logs = COLLECTOR["service"]["pipelines"]["logs"]
    assert "otlphttp/loki" in logs["exporters"]
    assert COLLECTOR["exporters"]["otlphttp/loki"]["endpoint"] == "http://loki:3100/otlp"


def test_the_privacy_processors_run_before_loki():
    """Nothing past the collector holds content (ADR-014 d14): Loki is past it."""
    procs = COLLECTOR["service"]["pipelines"]["logs"]["processors"]
    assert procs.index("attributes/drop_content") < procs.index("batch")
    assert procs.index("transform/identity") < procs.index("batch")


# --- native metrics stay out of Prometheus (behaviour, on the real collector) ------------

pytestmark_docker = pytest.mark.skipif(not docker_available(), reason="docker unavailable")

NATIVE = ["claude_code.token.usage", "claude_code.cost.usage", "claude_code.session.count",
          "copilot_chat.tool.call.count", "gen_ai.client.token.usage", "gen_ai.client.operation.duration"]
KEPT = ["traces.span.metrics.calls", "stdtel.other.metric"]


@pytestmark_docker
@pytest.mark.parametrize("overlay", ["overlay-none.yaml", "overlay-langfuse.yaml"])
def test_native_metrics_are_dropped_and_ours_are_kept(overlay):
    with collector(overlay) as c:
        c.post("/v1/metrics", {"resourceMetrics": [{"resource": {"attributes": kv({"service.name": "x"})},
                                                    "scopeMetrics": [{"metrics": [metric(n) for n in NATIVE + KEPT]}]}]})
        out = c.wait_for(*KEPT)
    for name in KEPT:
        assert name in out, f"{name} should reach Prometheus"
    for name in NATIVE:
        assert f"-> Name: {name}\n" not in out and f"Name: {name}" not in out, f"{name} reached Prometheus"


# --- live: an event sent to the running collector lands in Loki ---------------------------

def _stack_up() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{LOKI_HOST_PORT}/ready", timeout=2).read()
        urllib.request.urlopen("http://127.0.0.1:8888/metrics", timeout=2).read()
        return True
    except Exception:                                   # noqa: BLE001 - environment, not logic
        return False


@pytest.mark.skipif(not _stack_up(), reason="local stack with Loki not running")
def test_an_event_reaches_loki_through_the_running_collector():
    """The whole hop, live. The probe is labelled `stdtel-probe`, which ADR-014
    decision 13 reserves and the loaders ignore."""
    probe = uuid.uuid4().hex
    body = {"resourceLogs": [{"resource": {"attributes": kv({"service.name": "stdtel-probe"})},
                              "scopeLogs": [{"logRecords": [{
                                  "timeUnixNano": str(time.time_ns()),
                                  "body": {"stringValue": "probe"},
                                  "attributes": kv({"event.name": "stdtel_probe", "stdtel.probe.id": probe})}]}]}]}
    req = urllib.request.Request("http://127.0.0.1:4318/v1/logs", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
    q = urllib.parse.urlencode({"query": '{service_name="stdtel-probe"}', "limit": 50,
                                "start": str(time.time_ns() - 600 * 10**9), "end": str(time.time_ns() + 10**9)})
    found = False
    for _ in range(30):
        data = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{LOKI_HOST_PORT}/loki/api/v1/query_range?{q}", timeout=5).read())
        if probe in json.dumps(data):
            found = True
            break
        time.sleep(1)
    assert found, "the probe event never reached Loki"
