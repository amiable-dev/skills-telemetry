"""The contract a foreign emitter has to meet (ADR-010 decision 7).

stdtel observes skills, sub-agents, compactions and turns. It *receives*
external spend, from a process it does not control, in a repository it does not
own. So the contract needs a gate the emitter's own CI can run, or it is a
document rather than an agreement.

The check exists in the shape it does because of one measured failure: a real
emitter records no cost at all for roughly two thirds of its calls. A checker
that only validated shape would pass that file and the warehouse would average
over the holes. Coverage is therefore part of the report, not an afterthought.
"""
import json

import pytest

from stdtel import conform


def span(name="std.artefact.activation", **attrs):
    return {"name": name,
            "attributes": [{"key": k, "value": {"stringValue" if isinstance(v, str)
                                                else "doubleValue" if isinstance(v, float)
                                                else "intValue": v}}
                           for k, v in attrs.items()]}


def payload(*spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def good(**over):
    a = {"std.artefact.kind": "external", "std.external.system": "llm-council",
         "std.external.operation": "consult", "std.external.cost_usd": 1.25}
    a.update(over)
    return span(**a)


def test_a_conforming_span_passes():
    r = conform.check(payload(good()))
    assert r.ok and not r.problems
    assert r.spans == 1


def test_the_checker_is_not_vacuous_on_an_empty_payload():
    """A gate that passes an empty file is how the policy artefact went
    uncollected for weeks (#21): exit 0 having checked nothing."""
    r = conform.check(payload())
    assert not r.ok
    assert any("no spans" in p.message for p in r.problems)


def test_an_attribute_outside_the_allowlist_is_named():
    """The allowlist is the whole contract. A stray key would be dropped at the
    collector and the emitter would never know why its data was thin."""
    r = conform.check(payload(good(**{"std.external.prompt": "secret"})))
    assert not r.ok
    assert any("std.external.prompt" in p.message for p in r.problems)


def test_content_bearing_attributes_fail_loudly():
    """Metadata only. An emitter is a foreign process and its payload is not ours
    to trust, so this must fail rather than be quietly scrubbed downstream."""
    r = conform.check(payload(good(**{"gen_ai.input.messages": "who said what"})))
    assert not r.ok


def test_a_wrong_span_name_is_refused():
    r = conform.check(payload(good()))
    assert r.ok
    r2 = conform.check(payload({"name": "llm_council.consult", "attributes": []}))
    assert not r2.ok
    assert any("std.artefact.activation" in p.message for p in r2.problems)


def test_an_unknown_kind_is_refused():
    r = conform.check(payload(span(**{"std.artefact.kind": "workflow"})))
    assert not r.ok
    assert any("workflow" in p.message for p in r.problems)


def test_an_external_span_must_name_its_system():
    """Spend with no emitter named inflates a total nobody can trace back."""
    r = conform.check(payload(span(**{"std.artefact.kind": "external",
                                      "std.external.operation": "consult"})))
    assert not r.ok
    assert any("std.external.system" in p.message for p in r.problems)


def test_an_explicitly_null_cost_is_refused_because_omitting_is_the_contract():
    """The measured failure this gate exists for. A null in the field is an
    emitter saying "I have a cost and it is nothing", which is not what it means.
    Omit the attribute and the absence is recorded as an absence (ADR-005)."""
    s = good()
    s["attributes"].append({"key": "std.external.cost_usd", "value": {}})
    r = conform.check(payload(s))
    assert not r.ok
    assert any("omit" in p.message for p in r.problems)


def test_a_cost_that_is_not_a_number_is_refused():
    r = conform.check(payload(good(**{"std.external.cost_usd": "1.25"})))
    assert not r.ok


def test_an_absent_cost_is_allowed_but_counted():
    """Absent is honest; the emitter may genuinely not know. What must not happen
    is that nobody notices two thirds of the file is missing it."""
    a = {"std.artefact.kind": "external", "std.external.system": "llm-council",
         "std.external.operation": "consult"}
    r = conform.check(payload(span(**a), good()))
    assert r.ok, "an absent cost is not a contract violation"
    assert r.cost_reported == 1 and r.external == 2
    assert "1 of 2" in r.summary()


def test_low_cost_coverage_is_surfaced_even_when_everything_passes():
    """A shape-only checker would have passed the real file that started this.
    The number has to reach the reader, or the gate teaches nothing."""
    a = {"std.artefact.kind": "external", "std.external.system": "llm-council"}
    r = conform.check(payload(*[span(**a) for _ in range(9)], good()))
    assert r.ok
    assert "10%" in r.summary() or "1 of 10" in r.summary()
    assert r.thin_cost_coverage


def test_a_session_id_is_optional_but_must_look_like_one_when_present():
    """Council runs outside a Claude session too, and that spend is real. But an
    id that is not a session id joins to nothing and is worse than none."""
    assert conform.check(payload(good(**{"session.id": "4122e12b-1908-4008-96ec-cc883b99c710"}))).ok
    r = conform.check(payload(good(**{"session.id": "no"})))
    assert not r.ok
    assert any("session.id" in p.message for p in r.problems)


def test_every_problem_names_the_span_it_came_from():
    """A file can carry hundreds of spans; "something failed" is not actionable."""
    r = conform.check(payload(good(), good(**{"std.external.nope": "x"})))
    assert not r.ok
    assert all(p.span_index is not None for p in r.problems)
    assert {p.span_index for p in r.problems} == {1}


def test_the_cli_exits_nonzero_on_a_violation(tmp_path, capsys):
    p = tmp_path / "spans.json"
    p.write_text(json.dumps(payload(good(**{"std.external.nope": "x"}))))
    assert conform.main([str(p)]) == 1
    assert "std.external.nope" in capsys.readouterr().err


def test_the_cli_exits_zero_on_a_clean_file(tmp_path, capsys):
    p = tmp_path / "spans.json"
    p.write_text(json.dumps(payload(good())))
    assert conform.main([str(p)]) == 0
    assert "1 span" in capsys.readouterr().out
