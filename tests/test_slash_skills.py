"""#144: a skill started by slash command is a skill invocation, and a loop skill
started that way opens its scope.

stdtel captured skills on `PreToolUse` for the Skill *tool*. A skill the user
types as `/name` never goes through that tool, so it was invisible: verified on
2026-09-30, where `/probe-echo` produced a turn and no skill activation while
Claude Code recorded `skill_activated`. A loop skill declared `telemetry.scope`
therefore never opened its container when started the usual way.

The transcript records both forms (shapes copied from real sessions):
  * `/probe-echo`: a user entry `<command-name>/probe-echo</command-name>`, then
    an isMeta entry on the same prompt beginning "Base directory for this skill:".
  * `/loop /epic-loop EPIC=1 …`: `<command-name>/loop</command-name>` with
    `<command-args>`. The harness activates only `loop`; `epic-loop` is its
    argument. By decision (#144), a catalogued skill named first in `/loop`'s
    arguments is recorded as invoked, with trigger `loop`, so the inference is
    labelled and can be filtered.

Only the command name and the first argument token are read. The rest of the
arguments, and the skill text in the meta entry, are never read into anything.
"""
from __future__ import annotations

import json

from stdtel.artefact import SCOPE_NAME
from stdtel.hooks import cli as hooks
from stdtel.state import SessionState
from stdtel.transcript import read_slice
from tests.test_scope_containment import activations, catalogue, run  # noqa: F401 - fixture

SECRET = "ARGS-ARE-CONTENT-7f3a"


def user(ts, content, prompt, meta=None):
    e = {"type": "user", "timestamp": ts, "promptId": prompt, "message": {"role": "user", "content": content}}
    if meta:
        e["isMeta"] = True
    return json.dumps(e)


def slash(ts, name, prompt, args=None, skill_meta=True):
    text = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
    if args is not None:
        text += f"\n<command-args>{args}</command-args>"
    rows = [user(ts, text, prompt)]
    if skill_meta:
        rows.append(user(ts, [{"type": "text", "text": f"Base directory for this skill: /x/{name}\n\n{SECRET}"}],
                         prompt, meta=True))
    return rows


def assistant(ts):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "claude-opus-5", "usage": {"input_tokens": 3, "output_tokens": 2}, "content": []}})


def write(path, rows):
    path.write_text("\n".join(rows) + "\n")
    return path


# --- reading the transcript ----------------------------------------------------------------------

def test_a_typed_skill_is_read_as_one(tmp_path):
    t = write(tmp_path / "t.jsonl", slash("2026-10-03T09:00:00.000Z", "probe-echo", "p1"))
    got = read_slice(t).slash_skills()
    assert [(s.name, s.trigger, s.prompt_id) for s in got] == [("probe-echo", "user-slash", "p1")]


def test_a_builtin_command_is_not_a_skill(tmp_path):
    """/clear and its kind have no skill-load entry after them."""
    t = write(tmp_path / "t.jsonl", slash("2026-10-03T09:00:00.000Z", "clear", "p1", skill_meta=False))
    assert read_slice(t).slash_skills() == []


def test_a_meta_entry_that_is_not_a_skill_load_does_not_count(tmp_path):
    """Bundled /loop writes its own instructions as a meta entry, starting with
    its heading, not the skill-load line. That is not a skill from the catalogue."""
    rows = slash("2026-10-03T09:00:00.000Z", "compact", "p1", skill_meta=False)
    rows.append(user("2026-10-03T09:00:00.000Z", [{"type": "text", "text": "# /compact — summarise"}], "p1", meta=True))
    assert read_slice(write(tmp_path / "t.jsonl", rows)).slash_skills() == []


def test_loop_names_its_wrapped_skill_and_reads_nothing_else(tmp_path):
    t = write(tmp_path / "t.jsonl", slash("2026-10-03T09:17:53.994Z", "loop", "p9",
                                          args=f"/epic-loop EPIC=1 {SECRET}", skill_meta=False))
    got = read_slice(t).slash_skills()
    assert [(s.name, s.trigger) for s in got] == [("epic-loop", "loop")]
    assert SECRET not in repr(got)


def test_loop_with_a_plain_prompt_wraps_nothing(tmp_path):
    t = write(tmp_path / "t.jsonl", slash("2026-10-03T09:00:00.000Z", "loop", "p1",
                                          args="check the build every hour", skill_meta=False))
    assert read_slice(t).slash_skills() == []


# --- emitting the activation, and the scope ------------------------------------------------------

def _stop(sid, tmp_path, rows, monkeypatch, branch="feature/STDTEL-11-one"):
    monkeypatch.setenv("STDTEL_BRANCH", branch)
    t = write(tmp_path / f"{sid}.jsonl", rows)
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(tmp_path), "transcript_path": str(t)}, exporter=exp)
    return [s for s in exp.get_finished_spans() if s.attributes.get("std.artefact.kind") == "skill"], \
        list(exp.get_finished_spans())


def test_a_typed_catalogued_skill_becomes_an_activation(catalogue, tmp_path, monkeypatch):
    skills, _ = _stop("sl-1", tmp_path, slash("2026-10-03T09:00:00.000Z", "formatter", "p1")
                      + [assistant("2026-10-03T09:00:01.000Z")], monkeypatch)
    assert len(skills) == 1
    a = skills[0].attributes
    assert (a["std.skill.name"], a["std.skill.version"], a["std.skill.trigger"]) == ("formatter", "1.0.0", "user-slash")
    assert a["std.prompt.id"] == "p1" and a["std.artefact.source"] == "transcript"


def test_a_typed_uncatalogued_skill_is_recorded_as_unversioned(catalogue, tmp_path, monkeypatch):
    """The same rule as a Skill tool call: the harness loaded a skill; the
    catalogue just does not know it."""
    skills, _ = _stop("sl-2", tmp_path, slash("2026-10-03T09:00:00.000Z", "probe-echo", "p1"), monkeypatch)
    assert [(s.attributes["std.skill.name"], s.attributes["std.skill.version"]) for s in skills] == \
        [("probe-echo", "unversioned")]


def test_a_typed_loop_skill_opens_its_scope(catalogue, tmp_path, monkeypatch):
    skills, spans = _stop("sl-3", tmp_path, slash("2026-10-03T09:00:00.000Z", "epic-loop", "p1")
                          + [assistant("2026-10-03T09:00:01.000Z")], monkeypatch)
    assert SessionState.load("sl-3").scope_name == "epic-loop"
    assert {s.attributes.get(SCOPE_NAME) for s in activations(spans)} == {"epic-loop"}


def test_a_loop_wrapped_catalogued_skill_is_invoked_with_trigger_loop(catalogue, tmp_path, monkeypatch):
    skills, spans = _stop("sl-4", tmp_path, slash("2026-10-03T09:17:53.994Z", "loop", "p9",
                                                  args=f"/epic-loop EPIC=1 {SECRET}", skill_meta=False)
                          + [assistant("2026-10-03T09:17:55.000Z")], monkeypatch)
    assert [(s.attributes["std.skill.name"], s.attributes["std.skill.trigger"]) for s in skills] == \
        [("epic-loop", "loop")]
    assert SessionState.load("sl-4").scope_name == "epic-loop"
    assert not any(SECRET in json.dumps(dict(s.attributes)) for s in spans)


def test_a_loop_wrapped_uncatalogued_name_is_not_a_skill(catalogue, tmp_path, monkeypatch):
    """Without the harness activating it, only the catalogue can say it is a skill."""
    skills, _ = _stop("sl-5", tmp_path, slash("2026-10-03T09:00:00.000Z", "loop", "p1",
                                              args="/not-a-skill now", skill_meta=False), monkeypatch)
    assert skills == []


def test_a_typed_skill_is_counted_once_across_stops(catalogue, tmp_path, monkeypatch):
    sid = "sl-6"
    rows = slash("2026-10-03T09:00:00.000Z", "formatter", "p1") + [assistant("2026-10-03T09:00:01.000Z")]
    first, _ = _stop(sid, tmp_path, rows, monkeypatch)
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "cwd": str(tmp_path), "transcript_path": str(tmp_path / f"{sid}.jsonl")},
               exporter=exp)
    again = [s for s in exp.get_finished_spans() if s.attributes.get("std.artefact.kind") == "skill"]
    assert len(first) == 1 and again == []



def test_a_typed_builtin_command_emits_nothing(catalogue, tmp_path, monkeypatch):
    skills, _ = _stop("sl-7", tmp_path, slash("2026-10-03T09:00:00.000Z", "clear", "p1", skill_meta=False),
                      monkeypatch)
    assert skills == []
