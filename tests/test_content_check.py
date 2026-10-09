"""ADR-014 decision 13: content-dropping is tested on each machine, not assumed.

`stdtel-doctor` sends one span and one event carrying a content-shaped attribute
with a random marker, and reads back what the stores hold. "The marker is not
there" is also what you see when the probe never arrived, so each probe carries
a positive control — an id that must be found — beside the marker that must not.
Pass needs both, in Tempo and in Loki. Anything else fails, loudly.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from stdtel import doctor


# --- the decision, separated from the network --------------------------------------------

def test_passes_only_when_the_probe_arrived_and_the_marker_did_not():
    c = doctor.judge_content_probe(tempo=(True, False), loki=(True, False))
    assert c.ok and "dropped" in c.detail


@pytest.mark.parametrize("store,result", [("tempo", (True, True)), ("loki", (True, True))])
def test_a_marker_in_either_store_fails_and_says_to_turn_the_flag_off(store, result):
    kw = {"tempo": (True, False), "loki": (True, False), store: result}
    c = doctor.judge_content_probe(**kw)
    assert not c.ok
    assert store.capitalize() in c.detail or store in c.detail
    assert "OTEL_LOG_TOOL_DETAILS" in c.remedy


@pytest.mark.parametrize("store", ["tempo", "loki"])
def test_a_probe_that_never_arrived_is_inconclusive_never_a_pass(store):
    """The failure this check exists to avoid: absence of evidence read as
    evidence of absence."""
    kw = {"tempo": (True, False), "loki": (True, False), store: (False, False)}
    c = doctor.judge_content_probe(**kw)
    assert not c.ok
    assert "never arrived" in c.detail or "could not confirm" in c.detail


# --- the probe itself, with the stores faked --------------------------------------------------

def test_the_probe_sends_a_marker_and_a_control_and_reads_both_stores():
    sent, markers = [], {}

    def send(path, body):
        sent.append((path, body))
        blob = json.dumps(body)
        markers["marker"] = next(v["value"]["stringValue"] for rs in body.get("resourceSpans", body.get("resourceLogs", []))
                                 for ss in rs.get("scopeSpans", rs.get("scopeLogs", []))
                                 for rec in ss.get("spans", ss.get("logRecords", []))
                                 for v in rec["attributes"] if v["key"] == "tool_input")
        markers["id"] = next(v["value"]["stringValue"] for rs in body.get("resourceSpans", body.get("resourceLogs", []))
                             for ss in rs.get("scopeSpans", rs.get("scopeLogs", []))
                             for rec in ss.get("spans", ss.get("logRecords", []))
                             for v in rec["attributes"] if v["key"] == "stdtel.probe.id")
        assert '"stdtel-probe"' in blob, "probes must be labelled so the loaders ignore them"

    def tempo(probe_id):            # a collector that dropped the content
        return f"trace with {probe_id} and nothing else"

    def loki(probe_id):
        return f"log with {probe_id}"

    c = doctor.probe_content(send=send, read_tempo=tempo, read_loki=loki, attempts=1, wait=0)
    assert c.ok, c.detail
    assert {p for p, _ in sent} == {"/v1/traces", "/v1/logs"}
    assert markers["marker"] != markers["id"]


def test_the_probe_catches_a_leak():
    holder = {}

    def send(path, body):
        holder["body"] = json.dumps(body)

    def leaky(probe_id):            # a collector that forwarded everything
        return holder["body"]

    c = doctor.probe_content(send=send, read_tempo=leaky, read_loki=lambda i: f"{i}", attempts=1, wait=0)
    assert not c.ok and "Tempo" in c.detail


def test_an_empty_store_is_inconclusive_not_clean():
    """A store that answers but holds nothing must not read as "dropped"."""
    c = doctor.probe_content(send=lambda p, b: None, read_tempo=lambda i: f"{i}",
                             read_loki=lambda i: '{"data":{"result":[]}}', attempts=2, wait=0)
    assert not c.ok and "Loki" in c.detail


def test_a_collector_that_is_down_fails_rather_than_passing():
    def down(path, body):
        raise ConnectionRefusedError("nothing on 4318")
    c = doctor.probe_content(send=down, read_tempo=lambda i: i, read_loki=lambda i: i, attempts=1, wait=0)
    assert not c.ok and "could not send" in c.detail


# --- when it runs ------------------------------------------------------------------------------

def test_with_every_content_flag_off_there_is_nothing_to_prove(monkeypatch):
    monkeypatch.setattr(doctor, "_content_flags_on", lambda: [])
    c = doctor.content_dropped()
    assert c.ok and "off" in c.detail


@pytest.mark.parametrize("flag", ["OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES"])
def test_any_content_flag_on_runs_the_probe(monkeypatch, flag):
    """#164 council: the detailed view is not the only setting that sends content.
    Prompt logging and assistant responses do too, and a pass that only looked at
    one of them was a false privacy assurance."""
    monkeypatch.setattr(doctor, "_content_flags_on", lambda: [flag])
    monkeypatch.setattr(doctor, "probe_content", lambda **kw: doctor.Check("content dropped", False, "probe ran", "r"))
    assert doctor.content_dropped().detail == "probe ran"


def test_raw_api_bodies_fail_without_a_probe(monkeypatch):
    """A raw body can arrive as the record body, which the collector's attribute
    rules never touch, and nothing here has verified its shape. No probe can prove
    it dropped, so it fails rather than passing on a probe that tested other keys."""
    monkeypatch.setattr(doctor, "_content_flags_on", lambda: ["OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_RAW_API_BODIES"])
    monkeypatch.setattr(doctor, "probe_content", lambda **kw: doctor.Check("content dropped", True, "probe passed"))
    c = doctor.content_dropped()
    assert c.outcome == "fail" and "OTEL_LOG_RAW_API_BODIES" in c.detail and "OTEL_LOG_RAW_API_BODIES" in c.remedy


def test_the_probe_carries_the_assistant_response_key():
    assert "response" in doctor.PROBE_CONTENT_KEYS


def test_the_content_flags_are_the_ones_install_refuses_to_write():
    from tests.test_native_install import CONTENT_FLAGS
    assert set(doctor.CONTENT_FLAGS) == set(CONTENT_FLAGS)


@pytest.mark.parametrize("flag", ["OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_USER_PROMPTS"])
def test_the_flags_are_read_from_claude_codes_own_settings(tmp_path, monkeypatch, flag):
    from pathlib import Path
    home = Path.home()                               # conftest's throwaway HOME
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for f in doctor.CONTENT_FLAGS:
        monkeypatch.delenv(f, raising=False)
    assert doctor._content_flags_on() == []
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {flag: "1"}}))
    assert doctor._content_flags_on() == [flag]
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": {flag: "0"}}))
    assert doctor._content_flags_on() == []
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({"env": {flag: "true"}}))
    assert doctor._content_flags_on() == [flag], "a project's local settings turn it on too"
    monkeypatch.setenv("OTEL_LOG_ASSISTANT_RESPONSES", "1")
    assert set(doctor._content_flags_on()) == {flag, "OTEL_LOG_ASSISTANT_RESPONSES"}


def test_it_is_one_of_the_doctors_checks_and_has_a_flag():
    assert doctor.content_dropped in doctor.CHECKS
    import inspect
    assert "--content-check" in inspect.getsource(doctor.main)


def test_the_loader_ignores_probe_spans():
    """A probe must never become a row. Decision 13 reserves the service name."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("lt_probe", Path(__file__).resolve().parent.parent / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    span = {"name": "std.artefact.activation", "spanId": "s", "traceId": "t",
            "startTimeUnixNano": "1", "endTimeUnixNano": "2",
            "attributes": [{"key": "std.artefact.kind", "value": {"stringValue": "turn"}},
                           {"key": "std.prompt.id", "value": {"stringValue": "p"}}]}
    trace = {"batches": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "stdtel-probe"}}]},
                          "scopeSpans": [{"spans": [span]}]}]}
    acts, rows, sessions = mod.collect({"t": 1}, lambda _: trace)
    assert acts == [] and rows == [] and sessions == []


# --- live, against the running stack -----------------------------------------------------------

def _stack_up() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11010/ready", timeout=2).read()
        urllib.request.urlopen("http://127.0.0.1:3200/ready", timeout=2).read()
        return True
    except Exception:                                   # noqa: BLE001
        return False


@pytest.mark.skipif(not _stack_up(), reason="local stack with Loki and Tempo not running")
def test_the_running_collector_drops_content(monkeypatch):
    """The real check, end to end: the shared collector, Tempo and Loki."""
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    c = doctor.probe_content()
    assert c.ok, f"{c.detail} — {c.remedy}"
