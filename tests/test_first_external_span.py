"""Two defects found by the first real external span (llm-council, 2026-09-28).

Both had tests around them and neither was covered: the loader's unknown-key
signal had never met a span that passed through the Langfuse overlay, and the
documented `mise run load` had never been run since #68 made it need arguments.
"""
from __future__ import annotations

import collections
import importlib.util
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def loader():
    spec = importlib.util.spec_from_file_location("load_traces_fx", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def overlay_added_keys() -> set[str]:
    text = (ROOT / "collector" / "overlay-langfuse.yaml").read_text()
    return set(re.findall(r'set\(attributes\["([^"]+)"\]', text))


def test_the_overlay_adds_keys_at_all():
    """Guards the next test against passing vacuously on an empty set."""
    assert "langfuse.session.id" in overlay_added_keys()


def test_keys_our_own_collector_adds_are_not_reported_as_the_emitters():
    """The first real council span was reported as carrying an unknown
    `langfuse.session.id`. Council never sent it: our Langfuse overlay copies
    session.id there before Tempo. A warning that fires on every span whenever
    the overlay is on is one people learn to ignore — the opposite of its job.
    Derived from the overlay itself, so a key added there later is covered."""
    mod = loader()
    attrs = {"std.artefact.kind": "external", "std.external.system": "c",
             **{k: "x" for k in overlay_added_keys()}}
    kv = [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]
    trace = {"batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{
        "name": "std.artefact.activation", "spanId": "AAAAAAAAAAE=", "traceId": "AAAAAAAAAAAAAAAAAAAAAQ==",
        "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000", "attributes": kv}]}]}]}
    unknown = collections.Counter()
    mod.collect({"t": 1}, lambda _: trace, unknown=unknown)
    assert not unknown, f"reported our own collector's keys as the emitter's: {dict(unknown)}"


def test_a_genuinely_unknown_key_is_still_reported_beside_them():
    """The exemption must not swallow the signal it sits next to."""
    mod = loader()
    kv = [{"key": k, "value": {"stringValue": "x"}} for k in
          ("std.artefact.kind", "langfuse.session.id", "std.external.requests_made")]
    kv[0]["value"] = {"stringValue": "external"}
    trace = {"batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{
        "name": "std.artefact.activation", "spanId": "AAAAAAAAAAE=", "traceId": "AAAAAAAAAAAAAAAAAAAAAQ==",
        "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000", "attributes": kv}]}]}]}
    unknown = collections.Counter()
    mod.collect({"t": 1}, lambda _: trace, unknown=unknown)
    assert unknown == {"std.external.requests_made": 1}


def _load_args(**env) -> str:
    """LOAD_ARGS as the load recipe's shell would expand it — and nothing else.

    Not `make -n load`: GNU make executes any recipe line containing $(MAKE)
    even under -n, so a dry run of `load` really runs the loader against the
    real warehouse. An earlier draft of this test did exactly that.
    """
    clean = {k: v for k, v in os.environ.items() if k not in ("STDTEL_DSN", "STDTEL_TEMPO", "LOAD_ARGS")}
    probe = "print-load-args: ; @echo $(LOAD_ARGS)\n"
    return subprocess.run(["make", "--no-print-directory", "-f", "Makefile", "-f", "-", "print-load-args"],
                          cwd=ROOT, input=probe, capture_output=True, text=True,
                          env={**clean, **env}).stdout


def test_make_load_passes_the_arguments_the_loader_requires():
    """`LOAD_ARGS` was never defined anywhere, so the documented `mise run load`
    exited with a usage error — and `load-watch` wraps it in `|| true`, so the
    loop failed every fifteen minutes without a word, since #68."""
    out = _load_args()
    assert "--tempo http://localhost:3200" in out, out
    assert "--dsn postgresql://postgres:stdtel@localhost:5432/stdtel" in out, out


def test_make_load_honours_the_same_variables_as_the_compose_loader():
    out = _load_args(STDTEL_DSN="postgresql://u@db:5432/x", STDTEL_TEMPO="http://t:3200")
    assert "--dsn postgresql://u@db:5432/x" in out and "--tempo http://t:3200" in out, out
