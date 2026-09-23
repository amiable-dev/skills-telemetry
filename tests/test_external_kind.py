"""Spend the harness cannot see (ADR-010 decision 7).

A process the agent shells out to, or reaches as an MCP server, can spend real
money on models of its own. Nothing in a hook payload knows the amount: stdtel
sees that a command ran. Only the spending process can report it, which makes
this the one kind stdtel receives rather than observes — and the only place in
the schema where a currency amount is recorded.

Written before the emitter exists, so these tests define the contract the
emitter has to meet rather than describe one that already works.
"""
import pytest

from stdtel import artefact


def test_external_is_a_kind():
    assert artefact.KIND_EXTERNAL == "external"
    assert artefact.KIND_EXTERNAL in artefact.KINDS


def test_the_kind_list_stays_closed():
    """A closed enum is what makes one span name safe (ADR-009): the per-kind
    allowlist is the whole contract, and an unlisted kind has no allowlist."""
    assert set(artefact.KINDS) == {"skill", "subagent", "compaction", "turn", "external"}


def test_an_external_activation_carries_what_only_the_emitter_knows():
    a = artefact.external(system="llm-council", operation="consult", cost_usd=1.25,
                          requests=7, duration_ms=41000, model="anthropic/claude-opus-5",
                          started_at=1.0, ended_at=42.0,
                          usage_attrs={"gen_ai.usage.input_tokens": 900})["attributes"]
    assert a["std.artefact.kind"] == "external"
    assert a["std.external.system"] == "llm-council"
    assert a["std.artefact.name"] == "llm-council", "the bounded name a dashboard groups by"
    assert a["std.external.operation"] == "consult"
    assert a["std.external.cost_usd"] == 1.25
    assert a["std.external.requests"] == 7
    assert a["gen_ai.request.model"] == "anthropic/claude-opus-5"
    assert a["gen_ai.usage.input_tokens"] == 900


def test_an_unobserved_cost_is_omitted_not_zeroed():
    """The defect this contract exists to prevent. Two thirds of one real
    emitter's records carry no cost at all; a zero there would be averaged over
    and read as "this call was free" (ADR-005)."""
    a = artefact.external(system="llm-council", operation="consult",
                          started_at=1.0, ended_at=2.0)["attributes"]
    assert "std.external.cost_usd" not in a
    assert "std.external.requests" not in a


def test_a_zero_cost_is_kept_because_it_is_a_measurement():
    """Free tiers and cached responses really do cost nothing. Zero observed and
    nothing observed are different claims and must not collapse."""
    a = artefact.external(system="x", operation="y", cost_usd=0.0,
                          started_at=1.0, ended_at=2.0)["attributes"]
    assert a["std.external.cost_usd"] == 0.0


def test_a_system_is_required():
    """An external span with no emitter named is unattributable spend — worse
    than no span, because it inflates a total nobody can trace."""
    with pytest.raises(ValueError):
        artefact.external(system="", operation="consult", started_at=1.0, ended_at=2.0)


def test_the_allowlist_admits_no_free_text():
    """An emitter is a foreign process and its payload is not ours to trust. The
    prompt, the response and the file list must have no way through."""
    for forbidden in ("std.external.prompt", "gen_ai.input.messages", "std.external.args",
                      "tool.input", "std.external.question"):
        assert forbidden not in artefact.ALLOWED[artefact.KIND_EXTERNAL]


def test_an_external_span_may_be_scoped_like_anything_else():
    """The point of the kind: council spend inside a loop iteration is that
    iteration's cost, and rolls up with everything else."""
    assert artefact.SCOPE_KEYS <= artefact.ALLOWED[artefact.KIND_EXTERNAL]


def test_a_session_id_is_allowed_but_not_required():
    """Council runs outside a Claude session too — its own CLI, CI, a server —
    and that spend is real. A span with no session is honest missing data; one
    that is never emitted is a total that cannot reconcile against the bill."""
    assert "session.id" in artefact.ALLOWED[artefact.KIND_EXTERNAL]
    a = artefact.external(system="llm-council", operation="consult",
                          started_at=1.0, ended_at=2.0)["attributes"]
    assert "session.id" not in a, "the emitter supplies it when it has one"


def test_disallowed_attributes_are_dropped_and_reported(capsys):
    a = artefact.external(system="llm-council", operation="consult", started_at=1.0,
                          ended_at=2.0, usage_attrs={"std.external.secret": "x"})["attributes"]
    assert "std.external.secret" not in a
    assert "std.external.secret" in capsys.readouterr().err
