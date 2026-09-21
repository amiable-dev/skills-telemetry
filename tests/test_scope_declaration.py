"""`telemetry.scope`: the one thing an artefact knows that the harness cannot.

ADR-010 needs to know which unit of work an artefact runs in — a loop skill
works ticket-by-ticket, a formatting skill turn-by-turn, and nothing in a hook
payload distinguishes them. ADR-011 makes that a declaration and nothing more:
no gate, no validation failure, no degraded status for artefacts that never opt
in. The scar is ADR-004's `policy_ids` gate, where authors with nothing to cite
named an unrelated policy to get past it and the scorecard scored them against
a rule they had nothing to do with.
"""
from pathlib import Path

import pytest

from stdtel.manifest import DEFAULT_SCOPE, SCOPES, ManifestError, fill_gaps, parse_manifest

BASE = """---
name: {name}
description: d
metadata:
  version: "1.0.0"
  standard_id: STD-TEL-001
  policy_ids: "a.b"
  owner: platform
  harness_support: "claude-code"
{extra}---

body
"""


def manifest(name="s", path=None, **tel):
    extra = "".join(f"  telemetry.{k}: {v}\n" for k, v in tel.items())
    return parse_manifest(BASE.format(name=name, extra=extra), path=path)


def test_an_artefact_that_declares_nothing_is_turn_scoped_and_still_measured():
    """The rule that makes ADR-011 an observation system rather than a standard."""
    m = manifest()
    assert m.scope == "", "the raw declaration must stay empty, not be back-filled"
    assert m.effective_scope() == DEFAULT_SCOPE == "turn"


def test_declaring_turn_is_distinguishable_from_declaring_nothing():
    """They behave identically, and must not be stored identically: an overlay may
    only fill what was never stated, and the adoption count that bounds every
    rollup is "how many declared" — a defaulted value inflates it to all of them."""
    assert manifest(scope="turn").scope == "turn"
    assert manifest().scope == ""


def test_a_ticket_scoped_skill_is_read_as_such():
    assert manifest(scope="ticket").effective_scope() == "ticket"


def test_scope_is_case_and_space_insensitive():
    """Hand-written YAML. Rejecting ` Ticket` would teach nothing useful."""
    assert manifest(scope=" Ticket ").scope == "ticket"


def test_session_is_rejected_with_the_reason_rather_than_a_bare_enum():
    """It is the container ADR-010 rejects outright, and exactly what the author
    of a long-running skill reaches for. A bare "must be one of [...]" leaves
    them to guess why the obvious answer is wrong."""
    with pytest.raises(ManifestError) as e:
        manifest(scope="session")
    msg = str(e.value)
    assert "resumed and persists" in msg and "not a unit of work" in msg


def test_an_unknown_scope_fails_rather_than_being_silently_defaulted():
    """Defaulting a typo to `turn` would hide a ticket-scoped skill from every
    rollup it should appear in, with nothing to notice it by."""
    with pytest.raises(ManifestError):
        manifest(scope="epic")


def test_session_is_not_in_the_enum_at_all():
    assert "session" not in SCOPES and SCOPES == {"turn", "ticket"}


def test_an_overlay_may_supply_a_scope_the_artefact_never_stated():
    """The mechanism that makes "nothing is required of any author" affordable:
    a third-party skill nobody here owns can still be scoped, without editing a
    file the next upstream release overwrites (ADR-004)."""
    base = manifest(name="third-party")
    # a real overlay is loaded from a file, and `fill_gaps` records that file as
    # the provenance of anything it supplied
    over = manifest(name="third-party", path=Path("/overlay/third-party/SKILL.md"), scope="ticket")
    merged = fill_gaps(base, over)
    assert merged.effective_scope() == "ticket"
    assert merged.overlay_path is not None, "provenance: this was assumed, not observed"


def test_an_overlay_never_overrides_a_scope_the_artefact_declares():
    """If it did, the day upstream starts declaring its own scope our data would
    keep reporting the pinned one — stability that does not exist."""
    base = manifest(name="owned", scope="turn")
    over = manifest(name="owned", scope="ticket")
    assert fill_gaps(base, over).scope == "turn"
