from pathlib import Path
import pytest
from stdtel.manifest import parse_manifest, load_catalogue, ManifestError, cli

GOOD = """---
name: x
version: 1.0.0
standard_id: STD-LOG-001
policy_ids: [a.b]
owner: team
harness_support: [claude-code]
---
body
"""

def test_parse_good():
    m = parse_manifest(GOOD)
    assert m.name == "x" and m.version == "1.0.0"
    assert m.as_attributes()["std.policy.ids"] == "a.b"

@pytest.mark.parametrize("bad,msg", [
    (GOOD.replace("1.0.0", "v1"), "semver"),
    (GOOD.replace("STD-LOG-001", "LOG-1"), "standard_id"),
    (GOOD.replace("[a.b]", "[]"), "policy_ids"),
    (GOOD.replace("claude-code", "cursor"), "unknown harness"),
    (GOOD.replace("owner: team\n", ""), "missing required field: owner"),
])
def test_parse_bad(bad, msg):
    with pytest.raises(ManifestError, match=msg):
        parse_manifest(bad)

def test_repo_catalogue_valid():
    cat = load_catalogue(Path("skills"))
    assert "structured-logging" in cat
    assert cli(["skills"]) == 0

def test_duplicate_names(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir(); (tmp_path / d / "SKILL.md").write_text(GOOD)
    with pytest.raises(ManifestError, match="duplicate"):
        load_catalogue(tmp_path)

def test_lenient_catalogue_skips_bad_and_duplicate(tmp_path):
    (tmp_path / "good").mkdir(); (tmp_path / "good" / "SKILL.md").write_text(GOOD)
    (tmp_path / "dup").mkdir(); (tmp_path / "dup" / "SKILL.md").write_text(GOOD)
    (tmp_path / "bad").mkdir(); (tmp_path / "bad" / "SKILL.md").write_text("no front-matter")
    cat = load_catalogue(tmp_path, strict=False)
    assert set(cat) == {"x"}
    assert cat["x"].path == tmp_path / "dup" / "SKILL.md"   # first by sort order

def test_symlinked_skill_dirs_are_found(tmp_path):
    """~/.claude/skills is assembled with symlinks; the walker must descend them."""
    real = tmp_path / "repo" / "skills" / "x"; real.mkdir(parents=True)
    (real / "SKILL.md").write_text(GOOD)
    catalogue = tmp_path / "claude-skills"; catalogue.mkdir()
    (catalogue / "x").symlink_to(real, target_is_directory=True)
    assert load_catalogue(catalogue)["x"].version == "1.0.0"


def test_symlink_cycle_terminates(tmp_path):
    d = tmp_path / "a"; d.mkdir()
    (d / "SKILL.md").write_text(GOOD)
    (d / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert set(load_catalogue(tmp_path)) == {"x"}


SPEC = """---
name: x
description: d
metadata:
  version: "1.0.0"
  standard_id: STD-LOG-001
  policy_ids: "a.b, c.d"
  owner: team
  harness_support: "claude-code, copilot-cli"
  telemetry.emit: "false"
  telemetry.success_signal: test
---
body
"""


def test_spec_conformant_metadata_form():
    """Agent Skills allows only six frontmatter keys; our contract lives in metadata."""
    m = parse_manifest(SPEC)
    assert m.version == "1.0.0" and m.standard_id == "STD-LOG-001"
    assert m.policy_ids == ["a.b", "c.d"]                 # comma string -> list
    assert m.harness_support == ["claude-code", "copilot-cli"]
    assert m.telemetry_emit is False and m.success_signal == "test"


def test_both_frontmatter_forms_agree():
    flat = parse_manifest(GOOD)
    spec = parse_manifest(SPEC.replace('policy_ids: "a.b, c.d"', 'policy_ids: "a.b"')
                              .replace('harness_support: "claude-code, copilot-cli"',
                                       'harness_support: "claude-code"'))
    assert flat.as_attributes() == spec.as_attributes()


def test_spec_form_still_enforces_the_contract():
    with pytest.raises(ManifestError, match="semver"):
        parse_manifest(SPEC.replace('version: "1.0.0"', 'version: "v1"'))
    with pytest.raises(ManifestError, match="unknown harness"):
        parse_manifest(SPEC.replace("copilot-cli", "cursor"))


def test_shipped_catalogue_is_spec_conformant():
    """Non-spec top-level keys hard-error on claude.ai upload."""
    from stdtel.manifest import SPEC_KEYS, split_front_matter
    for skill in Path("skills").glob("*/SKILL.md"):
        data, _ = split_front_matter(skill.read_text())
        assert set(data) <= SPEC_KEYS, f"{skill}: non-spec keys {set(data) - SPEC_KEYS}"
