"""Run the pinned collector image with this repository's config, and read back
what it would export.

Every pipeline's exporter is replaced by `debug` at detailed verbosity, so the
collector's own processing is what is tested, not the YAML. No bind mounts:
config goes in with `docker cp` and Docker picks the port, so this runs the same
on Colima (which shares only $HOME) and in CI.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "otel/opentelemetry-collector-contrib:0.118.0"

DEBUG_EVERYWHERE = """
exporters:
  debug/test: { verbosity: detailed }
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug/test]
    metrics:
      receivers: [otlp]
      exporters: [debug/test]
    logs:
      receivers: [otlp]
      exporters: [debug/test]
"""


#: The environment as it was at import, before conftest's per-test isolation
#: points HOME at a throwaway directory. The docker CLI finds its context (Colima's
#: socket, here) under ~/.docker, so without this every docker call made from
#: inside a test fell back to /var/run/docker.sock, which does not exist — while
#: the same call from a module-scoped fixture, which runs outside that isolation,
#: worked. Captured once, passed to every docker call, changing nothing else.
_DOCKER_ENV = dict(os.environ)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """A docker call that says why it failed. A bare CalledProcessError hid the
    daemon's message, which is the only useful part."""
    r = subprocess.run(args, capture_output=True, text=True, env=_DOCKER_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, env=_DOCKER_ENV).returncode == 0


class Collector:
    def __init__(self, name: str, base: str):
        self.name, self.base = name, base

    def post(self, path: str, body: dict) -> None:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()

    def output(self) -> str:
        return subprocess.run(["docker", "logs", self.name], capture_output=True, text=True,
                              env=_DOCKER_ENV).stderr

    def wait_for(self, *needles: str, timeout: float = 20.0) -> str:
        """Output once every needle appears, or whatever there is at timeout."""
        deadline = time.time() + timeout
        out = self.output()
        while time.time() < deadline and not all(n in out for n in needles):
            time.sleep(0.5)
            out = self.output()
        return out


@contextlib.contextmanager
def collector(overlay: str = "overlay-none.yaml", extra: str = DEBUG_EVERYWHERE):
    """The base config, the named overlay, then `extra` merged over both."""
    name = f"stdtel-collector-test-{uuid.uuid4().hex[:8]}"
    extra_file = Path(__file__).parent / f".{name}.yaml"
    extra_file.write_text(extra)
    try:
        _run(["docker", "create", "--name", name, "-p", "127.0.0.1::4318", IMAGE,
              "--config=/etc/otelcol-contrib/base.yaml", "--config=/etc/otelcol-contrib/mode.yaml",
              "--config=/etc/otelcol-contrib/test.yaml"])
        for src, dst in ((ROOT / "collector" / "otel-collector.yaml", "base.yaml"),
                         (ROOT / "collector" / overlay, "mode.yaml"), (extra_file, "test.yaml")):
            _run(["docker", "cp", str(src), f"{name}:/etc/otelcol-contrib/{dst}"])
        _run(["docker", "start", name])
        port = _run(["docker", "port", name, "4318/tcp"]).stdout.strip().split(":")[-1]
        c = Collector(name, f"http://127.0.0.1:{port}")
        c.wait_for("Everything is ready", timeout=15)
        yield c
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=_DOCKER_ENV)
        extra_file.unlink(missing_ok=True)


def kv(attrs: dict) -> list:
    return [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]


def metric(name: str, attrs: dict | None = None) -> dict:
    ns = str(time.time_ns())
    return {"name": name, "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                  "dataPoints": [{"asInt": "1", "timeUnixNano": ns,
                                                  "attributes": kv(attrs or {})}]}}
