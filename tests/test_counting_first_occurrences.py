"""ADR-016 W1 (#158): a first occurrence is counted, on the shipped images.

A skill runs a few times a day, so most spanmetrics series are born at 1 and
never move. `increase()` only counts a first occurrence when Prometheus has a
zero before it, which start-timestamp ingestion writes at the series' start time.

That start time has to be the series' own. Collector-contrib 0.118 stamps one
start time per resource, taken when the resource was first seen, so a series born
later carries a start time that is hours old: the zero lands outside any recent
window and the first occurrence reads 0 again (measured 2026-10-13: `probe-a` and
`probe-b`, sent 8 s apart, got the same start time). From 0.124 the connector
keeps a start time per series (contrib #35994), and it starts each new calls
series at 0 (#38537). Every case below sends its probe after the
collector has been running for longer than the query window, which is the state
a real collector is always in.

Real images, real configs (tests/stack_harness.py). Slow by design: each case
waits on real time, because `increase()` is a function of it.
"""
from __future__ import annotations

import json
import re
import time
import uuid

import pytest
import yaml

from tests.collector_harness import docker_available
from tests.stack_harness import COMPOSE, ROOT, stack

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker unavailable")

WINDOW = 30          # seconds; every probe is born after the collector has run longer than this
CALLS = "traces_span_metrics_calls_total"


def probe(stem: str) -> str:
    return f"w1-{stem}-{uuid.uuid4().hex[:6]}"


def calls(name: str) -> str:
    return f'{CALLS}{{std_skill_name="{name}"}}'


def increase(name: str, window: int = WINDOW) -> str:
    return f"sum(increase({calls(name)}[{window}s]))"


@pytest.fixture(scope="module")
def s():
    with stack() as st:
        # A resource the connector has known for longer than the window: from here on
        # every new series is "born later", as on a collector that has been up all day.
        st.activation("skill", probe("warmup"))
        st.wait_for_series(f'{CALLS}{{std_artefact_kind="skill"}}')
        time.sleep(WINDOW + 10)
        yield st


def test_a_first_occurrence_is_counted(s):
    name = probe("first")
    s.activation("skill", name)
    s.wait_for_series(calls(name))
    time.sleep(3)
    assert (s.value(increase(name)) or 0) > 0, "a skill that ran once must not read as never run"


def test_a_first_duration_is_counted(s):
    """The duration histogram's first occurrence. The connector starts a new calls
    series at 0 itself (contrib #38537), so `calls_total` counts on any path. It does
    not do that for the histogram, so the histogram's zero is Prometheus's start-time
    ingestion alone. Without the flag, or on the remote-write fallback, this reads 0
    (measured 2026-10-14, #161). This is the test that makes the flag load-bearing."""
    name = probe("duration")
    s.activation("skill", name)
    s.wait_for_series(f'traces_span_metrics_duration_milliseconds_count{{std_skill_name="{name}"}}')
    time.sleep(3)
    got = s.value(f'sum(increase(traces_span_metrics_duration_milliseconds_count{{std_skill_name="{name}"}}[{WINDOW}s]))')
    assert (got or 0) > 0, "a skill's first duration must reach the p50 panels"


def test_the_series_starts_at_zero_just_before_its_first_sample(s):
    """The zero is the series' own start, not the resource's: within seconds of the 1."""
    name = probe("zero")
    s.activation("skill", name)
    s.wait_for_series(calls(name))
    time.sleep(3)
    samples = s.query(f"{calls(name)}[{WINDOW}s]")["result"][0]["values"]
    (t0, v0), (t1, _) = samples[0], samples[1]
    assert float(v0) == 0, samples
    assert 0 <= t1 - t0 < 10, f"zero written {t1 - t0:.0f}s before the first sample: a resource start time"


@pytest.mark.parametrize("position", [0.1, 0.5, 0.9])
def test_a_first_occurrence_anywhere_in_the_window_is_counted(s, position):
    """position: where in the window the first occurrence falls (0 = its start, 1 = its end)."""
    name = probe(f"pos{int(position * 10)}")
    s.activation("skill", name)
    s.wait_for_series(calls(name))
    # anchored on the data, not the wall clock: the zero is at the series' start time
    t0 = float(s.query(f"{calls(name)}[{WINDOW}s]")["result"][0]["values"][0][0])
    at = t0 + (1 - position) * WINDOW
    time.sleep(max(0.0, at - time.time()) + 1)
    assert (s.value(increase(name), at=at) or 0) > 0, f"missed at {position:.0%} through the window"


def test_three_increments_count_three(s):
    name = probe("three")
    for _ in range(3):
        s.activation("skill", name)
        time.sleep(2)
    s.wait_for_series(calls(name))
    time.sleep(3)
    # increase() extrapolates (ADR-016 decision 4), so more than two, never equality
    assert s.value(increase(name)) > 2


def test_a_collector_restart_mid_window_starts_a_new_series_that_counts(s):
    name = probe("restart")
    s.activation("skill", name)
    s.wait_for_series(calls(name))
    time.sleep(2)
    s.restart_collector()
    s.activation("skill", name)
    deadline = time.time() + 30
    while time.time() < deadline and (s.value(increase(name, 2 * WINDOW)) or 0) <= 1.5:
        time.sleep(1)
    # one before the restart and one after; the restart must not erase the first
    assert s.value(increase(name, 2 * WINDOW)) > 1.5


def test_a_selector_on_a_span_dimension_needs_no_join(s):
    """Dimensions are datapoint attributes, so they are plain labels (decision 1)."""
    name = probe("label")
    s.activation("skill", name)
    s.wait_for_series(calls(name))
    (series,) = s.query(calls(name))["result"]
    assert series["metric"]["std_skill_name"] == name
    assert series["metric"]["__name__"] == CALLS


# --- every dashboard expression still resolves ---------------------------------------------------

def _prometheus_exprs() -> list[str]:
    exprs = []
    for f in sorted((ROOT / "deploy" / "grafana" / "provisioning" / "dashboards").glob("*.json")):
        stack_ = list(json.loads(f.read_text()).get("panels", []))
        while stack_:
            p = stack_.pop()
            stack_.extend(p.get("panels", []))
            for t in p.get("targets", []):
                ds = t.get("datasource") or p.get("datasource")
                uid = ds.get("uid") if isinstance(ds, dict) else ds
                if "expr" in t and str(uid).lower() != "loki":
                    exprs.append(t["expr"])
    return exprs


DASHBOARD_EXPRS = _prometheus_exprs()


def test_the_dashboards_have_prometheus_expressions_to_check():
    assert any(CALLS in e for e in DASHBOARD_EXPRS), "the parser found nothing: the check below is vacuous"


@pytest.fixture(scope="module")
def shaped(s):
    """One span of every kind the dashboards select on, including an error."""
    s.activation("skill", "w1-dash-skill")
    s.activation("skill", "w1-dash-skill", error=True)
    s.activation("subagent", "Explore", **{"std.subagent.type": "Explore"})
    s.activation("turn", "turn")
    s.activation("compaction", "auto")
    s.activation("external", "llm-council")
    s.wait_for_series(f'{CALLS}{{std_artefact_kind="external"}}')
    time.sleep(3)
    return s


@pytest.mark.parametrize("expr", DASHBOARD_EXPRS, ids=lambda e: e[:60])
def test_every_dashboard_expression_resolves_with_its_names_and_labels(shaped, expr):
    shaped.query(expr.replace("$__rate_interval", "1m").replace("$__range", "1h"))
    names = set(shaped.names())
    for metric in set(re.findall(r"\b(?:traces_span_metrics_\w+|prometheus_\w+)\b", expr)):
        assert metric in names, f"{metric} is not in Prometheus: names changed"
    labels = set(shaped.labels())
    for label in set(re.findall(r"\b(std_\w+|span_name|status_code)\b", expr)):
        assert label in labels, f"label {label} is not in Prometheus: labels changed"


# --- a batch that arrives late --------------------------------------------------------------------

def test_a_retried_delayed_batch_still_counts_its_first_occurrence():
    """Prometheus is down when the first occurrence is exported. The collector's
    retry delivers it later; its zero is then older than samples already in the
    head (the warm-up's), and must still be accepted."""
    with stack(start=False) as st:
        st.start_prometheus()
        st.start_collector()
        st.activation("skill", probe("warmup"))
        st.wait_for_series(f'{CALLS}{{std_artefact_kind="skill"}}')
        time.sleep(WINDOW + 10)
        st.stop_prometheus()
        name = probe("delayed")
        st.activation("skill", name)
        time.sleep(8)                                   # at least one failed export and a retry
        assert "Exporting failed. Will retry" in st.collector_log(), \
            "no export failed while Prometheus was down: this case tested nothing"
        st.start_prometheus()
        st.wait_for_series(calls(name), timeout=90)
        time.sleep(3)
        assert (st.value(increase(name, 2 * WINDOW)) or 0) > 0


def test_the_harness_runs_what_compose_ships():
    """Guard: the images under test are the pinned ones, and pinned by digest."""
    for svc in ("otel-collector", "prometheus"):
        assert "@sha256:" in COMPOSE[svc]["image"], f"{svc} is not pinned by digest"


def test_the_settings_decision_1_names_are_explicit():
    """The settings decision 1 names. The flag is also covered by behaviour: without
    it the duration histogram's first occurrence reads 0
    (test_a_first_duration_is_counted). The translation strategy named is 3.15.0's
    default, so only this check sees it, and an upgrade that changes the default
    cannot change what ships."""
    assert "--enable-feature=created-timestamp-zero-ingestion" in COMPOSE["prometheus"]["command"]
    assert "--web.enable-otlp-receiver" in COMPOSE["prometheus"]["command"]
    prom = yaml.safe_load((ROOT / "deploy" / "prometheus.yml").read_text())
    assert prom["otlp"]["translation_strategy"] == "UnderscoreEscapingWithSuffixes"
    coll = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())
    assert coll["exporters"]["otlphttp/prometheus"]["metrics_endpoint"].endswith("/api/v1/otlp/v1/metrics")
    assert "prometheusremotewrite" not in coll["exporters"]
