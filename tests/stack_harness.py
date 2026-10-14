"""Run the shipped collector and Prometheus together, on a private network, and
ask Prometheus what it counted (ADR-016 W1).

Both images, Prometheus's command line and both configs are read from `deploy/`
and `collector/`, so the test is of what ships, not a copy of it. Only three
things are overridden, all outside what is measured:
- traces go to spanmetrics alone (no Tempo here) and logs go to `debug`;
- spanmetrics flushes every second instead of every 30, so a test waits seconds;
- nothing binds a fixed host port: Docker picks Prometheus's.

Config goes in with `docker cp`, like collector_harness, so this runs on Colima.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

import yaml

from tests.collector_harness import _DOCKER_ENV, ROOT, _run, kv

COMPOSE = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())["services"]
COLLECTOR_IMAGE = COMPOSE["otel-collector"]["image"]
PROMETHEUS_IMAGE = COMPOSE["prometheus"]["image"]

OVERRIDES = """
connectors:
  spanmetrics:
    metrics_flush_interval: 1s
exporters:
  debug/test: { verbosity: basic }
service:
  pipelines:
    traces:
      exporters: [spanmetrics]
    logs:
      exporters: [debug/test]
"""


class Stack:
    def __init__(self, tag: str, net: str, collector: str, prometheus: str):
        self.tag, self.net, self.collector, self.prometheus = tag, net, collector, prometheus
        self.prom_url = self.otlp_url = ""

    # --- containers ---------------------------------------------------------------------------

    def _port(self, name: str, port: str) -> str:
        return _run(["docker", "port", name, port]).stdout.strip().splitlines()[0].split(":")[-1]

    def start_prometheus(self) -> None:
        _run(["docker", "start", self.prometheus])
        self.prom_url = f"http://127.0.0.1:{self._port(self.prometheus, '9090/tcp')}"
        _wait(lambda: _get(self.prom_url + "/-/ready") is not None, 30, "prometheus ready")

    def stop_prometheus(self) -> None:
        _run(["docker", "stop", self.prometheus])

    def start_collector(self) -> None:
        _run(["docker", "start", self.collector])
        self.otlp_url = f"http://127.0.0.1:{self._port(self.collector, '4318/tcp')}"
        _wait(lambda: "Everything is ready" in self.collector_log(), 30, "collector ready")

    def restart_collector(self) -> None:
        _run(["docker", "restart", self.collector])
        self.otlp_url = f"http://127.0.0.1:{self._port(self.collector, '4318/tcp')}"
        _wait(lambda: self.collector_log().count("Everything is ready") >= 2, 30, "collector restarted")

    def collector_log(self) -> str:
        return subprocess.run(["docker", "logs", self.collector], capture_output=True, text=True,
                              env=_DOCKER_ENV).stderr

    # --- traffic ------------------------------------------------------------------------------

    def activation(self, kind: str, name: str, *, error: bool = False, **attrs: str) -> None:
        """One `std.artefact.activation` span, shaped like the Stop hook's."""
        ns = time.time_ns()
        a = {"std.artefact.kind": kind, "std.artefact.name": name, "std.artefact.source": "hook",
             "std.harness": "claude-code", **attrs}
        if kind == "skill":
            a.setdefault("std.skill.name", name)
        span = {"traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16],
                "name": "std.artefact.activation", "kind": 1,
                "startTimeUnixNano": str(ns - 2_000_000_000), "endTimeUnixNano": str(ns),
                "attributes": kv(a)}
        if error:
            span["status"] = {"code": 2}
        body = {"resourceSpans": [{"resource": {"attributes": kv({"service.name": "stdtel"})},
                                   "scopeSpans": [{"spans": [span]}]}]}
        req = urllib.request.Request(self.otlp_url + "/v1/traces", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()

    # --- Prometheus ---------------------------------------------------------------------------

    def query(self, expr: str, at: float | None = None) -> dict:
        params = {"query": expr}
        if at is not None:
            params["time"] = f"{at:.3f}"
        body = _get(self.prom_url + "/api/v1/query?" + urllib.parse.urlencode(params))
        if body is None or body.get("status") != "success":
            raise AssertionError(f"query failed: {expr}: {body}")
        return body["data"]

    def value(self, expr: str, at: float | None = None) -> float | None:
        """The single value of an instant query, or None when it returns nothing."""
        result = self.query(expr, at)["result"]
        return float(result[0]["value"][1]) if result else None

    def wait_for_series(self, selector: str, timeout: float = 60) -> float:
        """Block until the series has a sample; return the wall time it was first seen."""
        _wait(lambda: self.query(selector)["result"], timeout, f"series {selector}")
        return time.time()

    def labels(self) -> list[str]:
        return _get(self.prom_url + "/api/v1/labels")["data"]

    def names(self) -> list[str]:
        return _get(self.prom_url + "/api/v1/label/__name__/values")["data"]


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            raw = r.read()
            return json.loads(raw) if raw[:1] == b"{" else {"status": "success"}
    except Exception:
        return None


def _wait(cond, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cond():
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {what}")


@contextlib.contextmanager
def stack(start: bool = True, metrics_overlay: str = "overlay-none.yaml"):
    """The shipped collector and Prometheus on a private network. Prometheus is
    reachable from the collector as `prometheus`, as it is in compose.
    `metrics_overlay` fills compose's STDTEL_METRICS_OVERLAY slot (ADR-016 W5)."""
    tag = uuid.uuid4().hex[:8]
    net, coll, prom = f"stdtel-w1-{tag}", f"stdtel-w1-collector-{tag}", f"stdtel-w1-prometheus-{tag}"
    overrides = ROOT / "tests" / f".{coll}.yaml"
    overrides.write_text(OVERRIDES)
    try:
        _run(["docker", "network", "create", net])
        _run(["docker", "create", "--name", prom, "--network", net, "--network-alias", "prometheus",
              "-p", "127.0.0.1::9090", PROMETHEUS_IMAGE, *COMPOSE["prometheus"]["command"]])
        _run(["docker", "cp", str(ROOT / "deploy" / "prometheus.yml"), f"{prom}:/etc/prometheus/prometheus.yml"])
        _run(["docker", "create", "--name", coll, "--network", net, "-p", "127.0.0.1::4318",
              COLLECTOR_IMAGE, "--config=/etc/otelcol-contrib/base.yaml",
              "--config=/etc/otelcol-contrib/mode.yaml", "--config=/etc/otelcol-contrib/metrics.yaml",
              "--config=/etc/otelcol-contrib/test.yaml"])
        for src, dst in ((ROOT / "collector" / "otel-collector.yaml", "base.yaml"),
                         (ROOT / "collector" / "overlay-none.yaml", "mode.yaml"),
                         (ROOT / "collector" / metrics_overlay, "metrics.yaml"), (overrides, "test.yaml")):
            _run(["docker", "cp", str(src), f"{coll}:/etc/otelcol-contrib/{dst}"])
        s = Stack(tag, net, coll, prom)
        if start:
            s.start_prometheus()
            s.start_collector()
        yield s
    finally:
        for name in (coll, prom):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=_DOCKER_ENV)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, env=_DOCKER_ENV)
        overrides.unlink(missing_ok=True)
