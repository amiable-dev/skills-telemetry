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
