"""The ADR-009 capture contract, tested without going near a hook.

One span name discriminated by `kind` only works if the per-kind allowlist is
real, so these tests are the other half of that decision.
"""
import pytest

from stdtel import artefact


def _keys(act):
    return set(act["attributes"])


def test_every_builder_stays_inside_its_kinds_allowlist():
    """The whole case for one span name rests on this holding."""
    built = [
        artefact.subagent("a1", "Explore", 1.0, 2.0, {"gen_ai.usage.input_tokens": 5},
                          llm_requests=3, tool_calls=2, model="claude-opus-5",
                          depth=1, parent_prompt_id="p1", duration_ms=1000),
        artefact.compaction("auto", 1.0, 1.0, tokens_before=900, tokens_after=100,
                            turns_since_previous=4),
        artefact.turn("p1", 1.0, 2.0, {"gen_ai.usage.output_tokens": 7}, llm_requests=2,
                      tool_calls=3, model="claude-opus-5", duration_ms=500,
                      hook_ms={"/usr/local/bin/cc-status": 12}, permission_mode="default"),
    ]
    for act in built:
        kind = act["kind"]
        stray = {k for k in _keys(act) if not artefact.allowed(kind, k)}
        assert not stray, f"{kind} emitted attributes outside its allowlist: {stray}"


def test_a_turn_carries_no_name_because_prompt_id_is_unbounded():
    """`std.artefact.name` is a spanmetrics dimension. A prompt id there would
    open a new time series per turn — the same defect as #42, one layer up."""
    act = artefact.turn("p1", 1.0, 2.0, {}, llm_requests=1)
    assert "std.artefact.name" not in act["attributes"]
    assert act["attributes"]["std.prompt.id"] == "p1"
    assert not artefact.allowed(artefact.KIND_TURN, "std.artefact.name")


def test_the_other_kinds_do_carry_a_bounded_name():
    assert artefact.subagent("a", "Explore", 1.0, 2.0, {}, 0, 0)["attributes"][
        "std.artefact.name"] == "Explore"
    assert artefact.compaction("auto", 1.0, 1.0)["attributes"]["std.artefact.name"] == "auto"


def test_an_attribute_belonging_to_another_kind_is_refused(capsys):
    act = artefact.activation(artefact.KIND_COMPACTION, 1.0, 1.0,
                              {"std.compaction.reason": "auto", "std.skill.name": "leaked"})
    assert "std.skill.name" not in act["attributes"]
    assert "std.skill.name" in capsys.readouterr().err, "a silent drop is the bug ADR-005 exists for"


def test_absent_measurements_are_omitted_never_zeroed():
    """A compaction that reports dropping 0 tokens reads as one that did
    nothing. ADR-005: missing data is its own category."""
    act = artefact.compaction("manual", 1.0, 1.0)
    assert "std.compaction.tokens_before" not in act["attributes"]
    assert "std.compaction.tokens_after" not in act["attributes"]
    assert "std.compaction.turns_since_previous" not in act["attributes"]


def test_a_turn_without_an_observed_prompt_id_is_not_an_activation():
    with pytest.raises(ValueError):
        artefact.turn("", 1.0, 2.0, {}, llm_requests=1)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        artefact.activation("workflow", 1.0, 2.0, {})


@pytest.mark.parametrize("command,expected", [
    ("/Users/someone/.config/iterm2/cc-status", "cc-status"),
    ('node "/Users/someone/dev/graft-hooks.cjs" stop', "graft-hooks-cjs"),
    ("/opt/homebrew/bin/uv run stdtel-hook stop", "stdtel-hook"),
    ("", "unknown"),
])
def test_hook_latency_is_keyed_by_basename_not_by_path(command, expected):
    """A path says where this developer keeps their dotfiles. The basename says
    which hook was slow, which is the question. An interpreter is skipped, or
    every scripted hook on the machine would share one series."""
    assert artefact.hook_latency_key(command) == f"std.turn.hook.{expected}.ms"


def test_the_source_of_a_value_is_recorded():
    """A sub-agent inferred from a directory scan is not the same measurement as
    one the harness reported, and a query must be able to tell them apart."""
    hooked = artefact.subagent("a", "Explore", 1.0, 2.0, {}, 0, 0)
    scanned = artefact.subagent("a", "Explore", 1.0, 2.0, {}, 0, 0,
                                source=artefact.SOURCE_TRANSCRIPT)
    assert hooked["attributes"]["std.artefact.source"] == "hook"
    assert scanned["attributes"]["std.artefact.source"] == "transcript"


def test_kinds_are_closed_and_named():
    """Adding a kind is a schema decision, not a patch: the per-kind allowlist is
    the whole contract that makes one span name safe, and an unlisted kind has no
    allowlist at all. `external` was added by ADR-010 decision 7."""
    assert artefact.KINDS == ("skill", "subagent", "compaction", "turn", "external")
    assert set(artefact.ALLOWED) == set(artefact.KINDS)


# --- ADR-010: containment is common to every kind -----------------------------

@pytest.mark.parametrize("kind", artefact.KINDS)
def test_every_kind_may_carry_the_scope(kind):
    """A sub-agent and a compaction inside a loop iteration are as much that
    iteration's cost as the skill that opened it, so the scope belongs in
    `_COMMON` rather than being repeated per kind — and repeating it is how one
    kind ends up silently unscoped."""
    assert artefact.SCOPE_KEYS <= artefact.ALLOWED[kind]


def test_stamping_rejects_an_attribute_that_is_not_a_scope():
    """`stamp_scope` runs after `_clean`, so it is the last thing between a typo
    and the wire. A key dropped silently here is a container that never appears."""
    acts = [{"attributes": {}}]
    with pytest.raises(ValueError):
        artefact.stamp_scope(acts, {"std.scope.nmae": "epic-loop"})


def test_stamping_nothing_leaves_activations_untouched():
    """The common case: no scope is open, and an unscoped span must not gain an
    empty-string scope that reads as a value in every group-by."""
    acts = [{"attributes": {"std.artefact.kind": "turn"}}]
    artefact.stamp_scope(acts, {})
    assert acts[0]["attributes"] == {"std.artefact.kind": "turn"}
