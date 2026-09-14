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


# --- #49: a contract gate that will not say which file broke ---

def _write(root, name: str, front: str):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{front}---\n\n# {name}\n")
    return d / "SKILL.md"


VALID = """name: good
description: fine
metadata:
  version: "1.0.0"
  standard_id: STD-OK-001
  policy_ids: "pkg.rule"
  owner: platform
  harness_support: "claude-code"
"""
NO_METADATA = "name: bare\ndescription: has nothing else\n"


def test_the_error_names_the_file(tmp_path):
    from stdtel.manifest import ManifestError, load_catalogue
    path = _write(tmp_path, "bare", NO_METADATA)
    with pytest.raises(ManifestError) as e:
        load_catalogue(tmp_path)
    assert str(path) in str(e.value), "a gate that will not say which file is a bisect, not a gate"


def test_every_invalid_manifest_is_reported_in_one_run(tmp_path):
    """Onboarding ten skills should take one run, not ten.

    Strict mode used to re-raise on the first failure, so each run revealed one
    more problem and every run's output looked the same as the last.
    """
    from stdtel.manifest import ManifestError, load_catalogue
    a = _write(tmp_path, "bare", NO_METADATA)
    b = _write(tmp_path, "alsobare", "name: alsobare\ndescription: nor this\n")
    with pytest.raises(ManifestError) as e:
        load_catalogue(tmp_path)
    message = str(e.value)
    assert str(a) in message and str(b) in message


def test_one_bad_manifest_still_fails_a_root_that_is_otherwise_valid(tmp_path):
    from stdtel.manifest import ManifestError, load_catalogue
    _write(tmp_path, "good", VALID)
    bad = _write(tmp_path, "bare", NO_METADATA)
    with pytest.raises(ManifestError) as e:
        load_catalogue(tmp_path)
    assert str(bad) in str(e.value)


def test_lenient_mode_is_unchanged_by_any_of_this(tmp_path):
    """Hooks must never let one broken SKILL.md silence every other skill."""
    from stdtel.manifest import load_catalogue
    _write(tmp_path, "good", VALID)
    _write(tmp_path, "bare", NO_METADATA)
    assert set(load_catalogue(tmp_path, strict=False)) == {"good"}


# --- policy_ids may be empty only when the skill does not claim a policy signal ---

NO_POLICIES = """name: unscored
description: nothing verifies this one
metadata:
  version: "1.0.0"
  standard_id: STD-OK-001
  policy_ids: ""
  owner: platform
  harness_support: "claude-code"
  telemetry.success_signal: {signal}
"""


def test_empty_policy_ids_is_allowed_when_the_signal_is_not_policy(tmp_path):
    """Some skills genuinely have no deterministic check.

    Requiring a policy id unconditionally forced people to name an unrelated
    policy to get past the gate — which `stdtel-onboard` explicitly forbids, and
    which is worse than an honest empty list: it makes the primary metric score a
    skill against a rule it has nothing to do with.
    """
    from stdtel.manifest import load_manifest
    path = _write(tmp_path, "unscored", NO_POLICIES.format(signal="manual"))
    m = load_manifest(path)
    assert m.policy_ids == [] and m.success_signal == "manual"


def test_empty_policy_ids_is_still_rejected_when_the_signal_is_policy(tmp_path):
    """`success_signal: policy` is a claim that policies verify it. Name them."""
    from stdtel.manifest import ManifestError, load_manifest
    path = _write(tmp_path, "unscored", NO_POLICIES.format(signal="policy"))
    with pytest.raises(ManifestError) as e:
        load_manifest(path)
    assert "success_signal" in str(e.value), "the message must say which claim conflicts"


def test_the_default_signal_still_requires_policies(tmp_path):
    """success_signal defaults to `policy`, so omitting it keeps the old rule."""
    from stdtel.manifest import ManifestError, load_manifest
    front = NO_POLICIES.format(signal="policy").replace("  telemetry.success_signal: policy\n", "")
    path = _write(tmp_path, "unscored", front)
    with pytest.raises(ManifestError):
        load_manifest(path)


# --- #52: a version is asserted; the hash is observed ---

BODY_A = """name: hashme
description: d
metadata:
  version: "1.0.0"
  standard_id: STD-OK-001
  policy_ids: "pkg.rule"
  owner: platform
  harness_support: "claude-code"
---

Always prefer the index over grep.
"""


def _skill(tmp_path, front_and_body: str):
    p = tmp_path / "hashme" / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + front_and_body)
    return p


def test_the_content_hash_changes_when_the_instruction_changes(tmp_path):
    """The failure with no symptom: a skill edited without a version bump makes
    the scorecard aggregate two different populations into one row."""
    from stdtel.manifest import load_manifest
    before = load_manifest(_skill(tmp_path, BODY_A)).content_hash
    after = load_manifest(_skill(tmp_path, BODY_A.replace("index over grep", "grep over the index"))).content_hash
    assert before and after and before != after


def test_editing_only_the_metadata_block_does_not_change_the_hash(tmp_path):
    """Otherwise onboarding a skill would read as a change to its guidance."""
    from stdtel.manifest import load_manifest
    before = load_manifest(_skill(tmp_path, BODY_A)).content_hash
    after = load_manifest(_skill(tmp_path, BODY_A.replace('"1.0.0"', '"2.0.0"'))).content_hash
    assert before == after


def test_the_hash_is_short_and_opaque(tmp_path):
    from stdtel.manifest import load_manifest
    import re
    assert re.fullmatch(r"[0-9a-f]{16}", load_manifest(_skill(tmp_path, BODY_A)).content_hash)


def test_the_hash_is_emitted_as_a_span_attribute(tmp_path):
    from stdtel.manifest import load_manifest
    m = load_manifest(_skill(tmp_path, BODY_A))
    assert m.as_attributes()["std.skill.content_hash"] == m.content_hash


def test_a_manifest_parsed_without_a_file_still_has_a_hash():
    """parse_manifest is used on text in tests and in the skill map."""
    from stdtel.manifest import parse_manifest
    assert parse_manifest("---\n" + BODY_A).content_hash
