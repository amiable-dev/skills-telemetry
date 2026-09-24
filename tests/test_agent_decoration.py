"""Sub-agents as measurable artefacts (ADR-011, phase 5 of #78).

A sub-agent's cost was recorded and could not be attributed to a version of
anything — exactly the state an un-onboarded skill was in before ADR-004. The
same `metadata:` block now works, with one important difference recorded in
ADR-011: a skill's `metadata` key is *specified*, and other keys hard-error on
upload; an agent's is merely *tolerated*, undocumented, and could regress in any
release. Hence `test_our_own_agent_still_loads_after_decoration`.
"""
import json
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks
from stdtel.manifest import load_agent_catalogue

ROOT = Path(__file__).resolve().parent.parent

AGENT = """---
name: {name}
description: d
tools: Bash, Read
{extra}---

You are an agent.
"""


def write_agent(root: Path, name: str, decorated=True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    extra = ""
    if decorated:
        extra = ('metadata:\n  version: "2.1.0"\n  standard_id: STD-TEL-001\n'
                 '  owner: platform-observability\n')
    p = root / f"{name}.md"
    p.write_text(AGENT.format(name=name, extra=extra))
    return p


def subagent_span(tmp_path, monkeypatch, agent_type: str):
    sid = f"agent-{agent_type}".replace(":", "-")
    sub = tmp_path / sid / "subagents"
    sub.mkdir(parents=True)
    run = sub / "agent-a1.jsonl"
    run.write_text(json.dumps(
        {"type": "assistant", "timestamp": "2026-09-15T10:00:01.000Z",
         "message": {"model": "claude-haiku-4-5-20251001", "content": [],
                     "usage": {"input_tokens": 3, "output_tokens": 5,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0}}}) + "\n")
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": agent_type, "spawnDepth": 1}))

    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    hooks.subagent_start({"session_id": sid, "agent_id": "a1", "agent_type": agent_type,
                          "prompt_id": "p1", "agent_transcript_path": str(run)})
    hooks.subagent_stop({"session_id": sid, "agent_id": "a1", "agent_type": agent_type,
                         "prompt_id": "p1", "agent_transcript_path": str(run)})
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(tmp_path), "transcript_path": str(tmp_path / f"{sid}.jsonl")},
               exporter=exp)
    spans = [s for s in exp.get_finished_spans()
             if s.attributes.get("std.artefact.kind") == "subagent"]
    assert spans, "fixture produced no sub-agent span"
    return spans[0]


def test_a_decorated_agents_contract_reaches_its_span(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(tmp_path / "agents"))
    write_agent(tmp_path / "agents", "researcher")
    a = subagent_span(tmp_path, monkeypatch, "researcher").attributes
    assert a["std.agent.version"] == "2.1.0"
    assert a["std.agent.owner"] == "platform-observability"
    assert a["std.standard_id"] == "STD-TEL-001"
    assert a["std.agent.content_hash"], "the one thing observed rather than asserted"


def test_an_undecorated_agent_contributes_nothing_rather_than_blanks(tmp_path, monkeypatch):
    """An empty string reads as a value in every group-by; absent reads as what
    it is (ADR-005). Most agents in the wild carry no metadata at all."""
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(tmp_path / "agents"))
    write_agent(tmp_path / "agents", "plain", decorated=False)
    a = subagent_span(tmp_path, monkeypatch, "plain").attributes
    assert "std.agent.owner" not in a and "std.standard_id" not in a
    assert a["std.agent.version"] == "unversioned", "which is itself the finding"


def test_an_agent_nobody_has_defined_still_records_its_cost(tmp_path, monkeypatch):
    """Built-in agents have no file anywhere. Their cost is real and must not be
    dropped because the catalogue cannot describe them."""
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(tmp_path / "empty"))
    a = subagent_span(tmp_path, monkeypatch, "Explore").attributes
    assert a["std.subagent.type"] == "Explore"
    assert a["gen_ai.usage.output_tokens"] == 5


def test_a_plugin_scoped_agent_resolves_to_its_bare_definition(tmp_path, monkeypatch):
    """`plugin:agent` is how a plugin-provided agent arrives, and the file is
    filed under the bare name — the same suffix rule skills use (ADR-004)."""
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(tmp_path / "agents"))
    write_agent(tmp_path / "agents", "scorecard")
    a = subagent_span(tmp_path, monkeypatch, "stdtel:scorecard").attributes
    assert a["std.agent.version"] == "2.1.0"


def test_an_overlay_decorates_an_agent_we_do_not_own(tmp_path, monkeypatch):
    """The mechanism that makes "nothing is required of any author" affordable
    for agents too: editing a third party's file lasts until its next release."""
    monkeypatch.setenv("STDTEL_AGENTS_ROOT", str(tmp_path / "agents"))
    write_agent(tmp_path / "agents", "third-party", decorated=False)
    stub = tmp_path / "overlay" / "third-party"
    stub.mkdir(parents=True)
    (stub / "SKILL.md").write_text(
        '---\nname: third-party\ndescription: d\nmetadata:\n  version: "9.9.9"\n'
        '  owner: platform-observability\n---\n\nnote\n')
    monkeypatch.setenv("STDTEL_SKILLS_OVERLAY", str(tmp_path / "overlay"))
    a = subagent_span(tmp_path, monkeypatch, "third-party").attributes
    assert a["std.agent.version"] == "9.9.9"
    assert a["std.agent.owner"] == "platform-observability"


def test_skill_md_is_not_loaded_as_an_agent(tmp_path, monkeypatch):
    """A directory holding both would otherwise load every skill as an agent,
    and a skill would arrive carrying std.agent.* on a sub-agent span."""
    d = tmp_path / "mixed" / "some-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text('---\nname: some-skill\ndescription: d\n---\n\nbody\n')
    write_agent(tmp_path / "mixed", "real-agent")
    assert set(load_agent_catalogue(tmp_path / "mixed")) == {"real-agent"}


# --- the tolerance this depends on, watched ----------------------------------

def test_our_own_agent_still_loads_after_decoration():
    """ADR-011 decision 3. `metadata` is not a documented agent front-matter key;
    it works because unknown keys are ignored, verified against 2.1.277. If that
    tolerance ever ends, sub-agents silently go back to arriving undecorated —
    so this asserts the block is both present and parseable, and the day it stops
    being accepted is a failing test rather than a quiet gap in the data."""
    text = (ROOT / "agents" / "skill-scorecard-analyst.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] and front["description"], "the documented keys must survive"
    assert front["metadata"]["version"], "our own agent is the canary for this"
    cat = load_agent_catalogue(ROOT / "agents")
    assert cat["skill-scorecard-analyst"].version == front["metadata"]["version"]
