"""ADR-016 W5 (#161): the rollback path is documented, switchable, and tested.

If start-timestamp ingestion over OTLP has to be abandoned, one variable moves the
collector's metrics back to remote write:
`STDTEL_METRICS_OVERLAY=overlay-remote-write.yaml`. Prometheus keeps its
remote-write receiver on, so no other edit is needed.

What the fallback costs, measured (#161): remote write 1.0 carries no start time,
so Prometheus writes no zero. The collector starts each new `calls_total` series at
0 itself (contrib #38537), so count panels still count a first occurrence. The
duration histogram gets no such zero, so the duration panels miss a skill's first
duration. Each rate panel says which of the two it is.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
import yaml

from tests.collector_harness import docker_available

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "local-stack.md").read_text()
COMPOSE = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())["services"]
OVERLAY = ROOT / "collector" / "overlay-remote-write.yaml"
BOARDS = sorted((ROOT / "deploy" / "grafana" / "provisioning" / "dashboards").glob("*.json"))


def _section(heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", DOC, re.S | re.M)
    assert m, f"docs/local-stack.md has no '## {heading}' section"
    return m.group(1)


# --- the procedure ---------------------------------------------------------------------------------

def test_the_rollback_section_states_each_rule():
    s = _section("Rolling back the metrics path")
    assert "last passing digest" in s, "an upgrade that fails W1 stays on the last digest that passed"
    assert "test_counting_first_occurrences" in s, "names the test that decides it"
    assert "STDTEL_METRICS_OVERLAY=overlay-remote-write.yaml" in s, "the fallback is one variable"
    for table in ("Skills invoked", "MCP calls", "Tool calls", "Spend by scope"):
        assert table in s, f"exact counts: the per-event table {table!r} is named"
    assert "per-event" in s
    assert "0.124" in s
    assert re.search(r"no\s+(threshold|query)[^.]*restores?\s+a\s+first\s+occurrence", s, re.I), \
        "says plainly that nothing restores a first occurrence"
    assert "mise run test" in s, "states that the gate tests the default, not the running fallback"


def test_compose_has_a_metrics_overlay_slot_after_the_collector_overlay():
    cmd = COMPOSE["otel-collector"]["command"]
    assert cmd[-1] == "--config=/etc/otelcol/${STDTEL_METRICS_OVERLAY:-overlay-none.yaml}"
    assert any("STDTEL_COLLECTOR_OVERLAY" in c for c in cmd[:-1]), "langfuse and the fallback combine"


def test_prometheus_keeps_both_receivers_so_the_fallback_is_one_variable():
    cmd = COMPOSE["prometheus"]["command"]
    assert "--web.enable-otlp-receiver" in cmd and "--web.enable-remote-write-receiver" in cmd


def test_the_overlay_switches_only_the_metrics_exporter():
    cfg = yaml.safe_load(OVERLAY.read_text())
    assert set(cfg) == {"exporters", "service"}
    assert cfg["exporters"]["prometheusremotewrite"]["endpoint"] == "http://prometheus:9090/api/v1/write"
    assert cfg["service"] == {"pipelines": {"metrics": {"exporters": ["prometheusremotewrite"]}}}


# --- every rate panel says what the fallback costs -------------------------------------------------

def _rate_panels():
    for f in BOARDS:
        for p in json.loads(f.read_text())["panels"]:
            if any(re.search(r"\b(rate|increase)\(", t.get("expr") or "") for t in p.get("targets", [])):
                yield f.name, p


RATE_PANELS = list(_rate_panels())


def test_there_are_rate_panels_to_check():
    assert len(RATE_PANELS) >= 7


@pytest.mark.parametrize("item", RATE_PANELS, ids=lambda i: f"{i[0]}/{i[1]['title'][:40]}")
def test_every_rate_panel_states_what_the_fallback_costs_it(item):
    """A duration panel reads histogram buckets, which get no zero from the
    connector: it undercounts on the fallback. A count panel does not."""
    _board, p = item
    d = p.get("description") or ""
    assert "first occurrence" in d and "remote-write fallback" in d, p["title"]
    assert "docs/local-stack.md#rolling-back-the-metrics-path" in d, p["title"]
    histogram = any("_bucket" in (t.get("expr") or "") for t in p["targets"])
    if histogram:
        assert "first duration is missing" in d, p["title"]
    else:
        assert "on either metrics path" in d, p["title"]


# --- the fallback, on the shipped images -----------------------------------------------------------

@pytest.mark.skipif(not docker_available(), reason="docker unavailable")
def test_the_fallback_keeps_the_dashboards_and_undercounts_as_documented():
    """One stack on the fallback. (a) Every dashboard expression still resolves with
    its names and labels, so a rollback breaks no panel. (b) A first count is still
    counted. (c) A first duration is not. If (c) ever starts counting, the duration
    panels' caveat can go. If (b) stops, the count panels need one."""
    from tests.stack_harness import stack
    from tests.test_counting_first_occurrences import CALLS, DASHBOARD_EXPRS

    with stack(metrics_overlay="overlay-remote-write.yaml") as s:
        for kind, name in (("skill", "rb-skill"), ("subagent", "Explore"), ("turn", "turn"),
                           ("compaction", "auto"), ("external", "llm-council")):
            s.activation(kind, name, **({"std.subagent.type": "Explore"} if kind == "subagent" else {}))
        s.activation("skill", "rb-skill", error=True)
        s.wait_for_series(f'{CALLS}{{std_artefact_kind="external"}}')
        time.sleep(4)
        names, labels = set(s.names()), set(s.labels())
        for expr in DASHBOARD_EXPRS:
            s.query(expr.replace("$__rate_interval", "1m").replace("$__range", "1h"))
            for metric in set(re.findall(r"\btraces_span_metrics_\w+", expr)):
                assert metric in names, f"fallback renames {metric}"
            for label in set(re.findall(r"\b(std_\w+|span_name|status_code)\b", expr)):
                assert label in labels, f"fallback loses label {label}"
        assert "Exporting failed" not in s.collector_log(), "the fallback exporter is not delivering"
        calls = s.value(f'sum(increase({CALLS}{{std_artefact_kind="compaction"}}[30s]))')
        durations = s.value('sum(increase(traces_span_metrics_duration_milliseconds_count'
                            '{std_artefact_kind="compaction"}[30s]))')
    assert (calls or 0) > 0, "the fallback lost the connector's zero: count panels need the caveat too"
    assert not durations, f"the fallback counted a first duration ({durations}): that caveat can go"
