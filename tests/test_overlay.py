"""Attributing a skill you do not own, without editing it (#51).

Onboarding a third-party skill by editing its SKILL.md works exactly once: the
next upstream release overwrites it, a plugin reinstall replaces the directory,
and a new machine has none of it. Each failure is silent — the skill still emits
spans, they just arrive `unversioned`, which looks identical to a skill nobody
has onboarded yet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FULL = """---
name: owned
description: a skill that states its own contract
metadata:
  version: "3.0.0"
  standard_id: STD-OWN-001
  policy_ids: "own.rule"
  owner: upstream
  harness_support: "claude-code"
---

Body of the real skill.
"""

BARE = """---
name: third-party
description: someone else's skill, with no contract at all
---

Body we do not control.
"""


def _write(root: Path, name: str, text: str) -> Path:
    p = root / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _stub(root: Path, name: str, metadata: str) -> Path:
    return _write(root, name, f"---\nname: {name}\n{metadata}---\n")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    skills, overlay = tmp_path / "skills", tmp_path / "overlay"
    skills.mkdir(); overlay.mkdir()
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("STDTEL_SKILLS_OVERLAY", str(overlay))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    return skills, overlay


def _catalogue():
    from stdtel.hooks.cli import _catalogue
    return _catalogue()


def test_an_overlay_attributes_a_skill_whose_file_is_untouched(roots):
    skills, overlay = roots
    original = _write(skills, "third-party", BARE)
    _stub(overlay, "third-party", 'metadata:\n  standard_id: STD-CTX-001\n  owner: platform\n')
    m = _catalogue()["third-party"]
    assert m.standard_id == "STD-CTX-001" and m.owner == "platform"
    assert original.read_text() == BARE, "the overlay must not touch the skill"


def test_the_skill_wins_over_the_overlay(roots):
    """The failure with no symptom.

    If the overlay overrode, then the day upstream starts declaring its own
    version our data would keep reporting the pinned one — stability that does
    not exist. An overlay fills gaps; it does not shadow.
    """
    skills, overlay = roots
    _write(skills, "owned", FULL)
    _stub(overlay, "owned", 'metadata:\n  version: "1.0.0"\n  standard_id: STD-WRONG-999\n')
    m = _catalogue()["owned"]
    assert m.version == "3.0.0" and m.standard_id == "STD-OWN-001"


def test_an_overlay_stub_may_omit_the_version(roots):
    """The operator usually has no honest version for someone else's artifact.

    `std.skill.content_hash` (#52) is what identifies it instead.
    """
    skills, overlay = roots
    _write(skills, "third-party", BARE)
    _stub(overlay, "third-party", 'metadata:\n  standard_id: STD-CTX-001\n')
    m = _catalogue()["third-party"]
    assert m.version == "unversioned"
    assert m.content_hash, "identity has to come from somewhere"


def test_a_skill_with_no_contract_still_reaches_the_catalogue(roots):
    """Lenient loading used to drop an invalid manifest entirely, so a skill
    nobody had onboarded had no hash either — nothing to attribute it by."""
    skills, _ = roots
    _write(skills, "third-party", BARE)
    m = _catalogue()["third-party"]
    assert m.version == "unversioned" and m.content_hash


def test_the_overlay_can_attribute_a_skill_with_no_file_at_all(roots):
    """A built-in, or a skill whose directory we cannot see."""
    _, overlay = roots
    _stub(overlay, "builtin", 'metadata:\n  standard_id: STD-BLT-001\n  owner: platform\n')
    m = _catalogue()["builtin"]
    assert m.standard_id == "STD-BLT-001" and m.content_hash == ""


def test_empty_contract_fields_are_not_emitted_as_empty_attributes(roots):
    """An empty std.standard_id is worse than an absent one: it reads as a value."""
    skills, _ = roots
    _write(skills, "third-party", BARE)
    attrs = _catalogue()["third-party"].as_attributes()
    assert "std.standard_id" not in attrs and "std.skill.owner" not in attrs
    assert attrs["std.skill.content_hash"]


def test_the_strict_gate_is_unaffected(tmp_path):
    """CI must still refuse a catalogue whose manifests do not meet the contract."""
    from stdtel.manifest import ManifestError, load_catalogue
    _write(tmp_path, "third-party", BARE)
    with pytest.raises(ManifestError):
        load_catalogue(tmp_path)
