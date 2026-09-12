"""The distribution artifacts of ADR-001: three loader manifests, one installer.

The manifests are generated from stdtel.install.EVENTS, so these tests exist to
catch drift between what the installer writes and what the repo ships.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stdtel.install import (EVENTS, InstallError, claude_hooks, copilot_hooks,
                            hook_binary, merge_settings)

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return json.loads((ROOT / rel).read_text())


# --- the installer resolves an absolute path, because hooks get a bare PATH ---

def test_hook_binary_is_absolute_and_beside_this_interpreter():
    b = hook_binary()
    assert b.is_absolute() and b.is_file()
    assert b.parent == Path(sys.executable).parent, "must not follow the venv symlink out"


def test_hook_binary_errors_loudly_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(InstallError, match="uv tool install"):
        hook_binary()


def test_rendered_hooks_carry_the_absolute_path():
    binary = hook_binary()
    entry = claude_hooks(binary)["hooks"]["SessionStart"][0]["hooks"][0]
    assert str(binary) in entry["command"]
    assert entry["command"].startswith("/"), "a bare name is not resolvable in a hook shell"


def test_exec_form_splits_command_and_args():
    binary = hook_binary()
    entry = claude_hooks(binary, exec_form=True)["hooks"]["Stop"][0]["hooks"][0]
    assert entry["command"] == str(binary) and entry["args"] == ["stop"]


def test_only_tool_events_get_a_matcher():
    hooks = claude_hooks(hook_binary())["hooks"]
    assert hooks["PreToolUse"][0]["matcher"] == "Skill"
    assert "matcher" not in hooks["SessionStart"][0]


# --- Copilot must not be recorded as claude-code ---

def test_copilot_hooks_stamp_the_harness():
    cop = copilot_hooks(hook_binary(), "copilot-cli")
    assert cop["version"] == 1
    for groups in cop["hooks"].values():
        assert groups[0]["hooks"][0]["env"]["STDTEL_HARNESS"] == "copilot-cli"


def test_copilot_uses_its_own_event_names():
    assert set(copilot_hooks(hook_binary())["hooks"]) == {e[3] for e in EVENTS}


# --- merging must not clobber a settings file the developer already has ---

def test_merge_preserves_unrelated_settings(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"KEEP": "1"}, "model": "opus", "hooks": {"Other": []}}))
    merged = merge_settings(target, claude_hooks(hook_binary()))
    assert merged["model"] == "opus" and merged["env"]["KEEP"] == "1"
    assert "Other" in merged["hooks"] and "SessionStart" in merged["hooks"]
    assert json.loads(target.read_text()) == merged


def test_merge_rejects_a_corrupt_settings_file(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        merge_settings(target, {"hooks": {}})


# --- the shipped manifests must match the code that generates them ---

def test_agent_plugins_manifest_is_conformant():
    """v1 closes the manifest: only these top-level fields are permitted."""
    permitted = {"$schema", "name", "version", "description", "author", "homepage",
                 "repository", "license", "keywords", "extensions"}
    p = _read("plugin.json")
    assert set(p) <= permitted, f"non-spec fields: {set(p) - permitted}"
    assert p["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert p["name"] == "stdtel"


def test_shipped_hook_manifests_match_what_the_installer_generates():
    """Compares the whole structure, not just event names.

    An earlier version compared names only, and so did not notice when
    PostToolUse stopped being matcher-restricted — the committed JSON kept a
    stale `matcher: Skill` that would have silenced tool-failure counting for
    every plugin install.
    """
    class Bare(str):
        def __str__(self):
            return "stdtel-hook"

    assert _read("hooks/hooks.json") == json.loads(
        json.dumps(claude_hooks(Bare("stdtel-hook"))))
    assert _read("com.github.copilot/hooks/hooks.json") == json.loads(
        json.dumps(copilot_hooks(Bare("stdtel-hook"), "copilot-vscode")))


def test_post_tool_use_is_not_matcher_restricted():
    """Tool-call failure rate needs every tool, not only Skill."""
    hooks = claude_hooks(hook_binary())["hooks"]
    assert "matcher" not in hooks["PostToolUse"][0]
    assert "matcher" not in hooks["PostToolUseFailure"][0]
    assert hooks["PreToolUse"][0]["matcher"] == "Skill", "PreToolUse stays cheap"


def test_shipped_copilot_manifest_sets_the_harness():
    for groups in _read("com.github.copilot/hooks/hooks.json")["hooks"].values():
        assert groups[0]["hooks"][0]["env"]["STDTEL_HARNESS"].startswith("copilot")


def test_versions_agree_across_manifests():
    import tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert _read("plugin.json")["version"] == version
    assert _read(".claude-plugin/plugin.json")["version"] == version
    assert _read(".claude-plugin/marketplace.json")["plugins"][0]["version"] == version


def test_skills_tree_is_where_every_loader_looks():
    assert list((ROOT / "skills").glob("*/SKILL.md")), "portable skills/ must be populated"


# --- agent + skill artifacts shipped with the plugin ---

def test_plugin_declares_its_components():
    """`skills` takes a directory; `agents` takes a list of files.

    This test previously asserted `agents == ["./agents/"]`, which is the value
    that failed installation — so it did not merely miss the bug, it pinned it.
    Schema validity is now checked by the real validator, below.
    """
    p = _read(".claude-plugin/plugin.json")
    assert p["skills"] == "./skills/"
    assert isinstance(p["agents"], list) and all(a.endswith(".md") for a in p["agents"])
    assert (ROOT / "agents").is_dir() and (ROOT / "skills").is_dir()


def test_every_agent_has_usable_front_matter():
    import yaml
    agents = list((ROOT / "agents").glob("*.md"))
    assert agents, "plugin declares ./agents/"
    for a in agents:
        text = a.read_text()
        assert text.startswith("---"), a
        meta = yaml.safe_load(text.split("---", 2)[1])
        assert meta["name"] == a.stem, f"{a}: name must match filename"
        # the description is what the model matches on; it must say WHEN to use it
        assert len(meta["description"]) > 60, f"{a}: description too thin to route on"
        assert "Use when" in meta["description"], f"{a}: description must state when to use it"


# --- the real schema, not just our own consistency ---

def _claude_cli():
    return shutil.which("claude")


@pytest.mark.skipif(_claude_cli() is None, reason="claude CLI not installed")
@pytest.mark.parametrize("target", [".", ".claude-plugin/plugin.json"])
def test_manifests_pass_the_real_validator(target):
    """`claude plugin validate` is the schema that actually gates installation.

    Our other tests check the manifests against our own EVENTS table, which is
    internal consistency, not validity. A manifest can be perfectly consistent
    with itself and still be rejected at install — `agents: ["./agents/"]` was,
    with "agents.0: Invalid input", and shipped because nothing checked.
    """
    r = subprocess.run([_claude_cli(), "plugin", "validate", target],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"{target}:\n{r.stdout}\n{r.stderr}"


def test_agents_field_lists_files_not_directories():
    """The schema wants each agent's file. A directory fails validation."""
    agents = _read(".claude-plugin/plugin.json").get("agents", [])
    assert agents, "the plugin ships an agent; it must be declared"
    for entry in agents:
        assert not entry.endswith("/"), f"{entry} is a directory; list the .md file"
        assert (ROOT / entry).is_file(), f"{entry} does not exist"


def test_every_agent_on_disk_is_declared():
    """Adding an agent without listing it ships a plugin missing that agent."""
    declared = {Path(a).name for a in _read(".claude-plugin/plugin.json").get("agents", [])}
    on_disk = {f.name for f in (ROOT / "agents").glob("*.md")}
    assert on_disk == declared, f"undeclared: {sorted(on_disk - declared)}"


def test_manifest_does_not_redeclare_conventional_component_paths():
    """Conventional paths are auto-discovered; declaring them loads them twice.

    `claude plugin validate` accepts `"hooks": "./hooks/hooks.json"`, and the
    plugin then fails at load with "Duplicate hooks file detected ... The
    standard hooks/hooks.json is loaded automatically, so manifest.hooks should
    only reference additional hook files."

    Validation is not loading. This rule is only observable by installing, which
    is why it reached a user twice.
    """
    manifest = _read(".claude-plugin/plugin.json")
    hooks = manifest.get("hooks")
    if hooks is not None:
        declared = [hooks] if isinstance(hooks, str) else hooks
        for entry in declared:
            assert Path(entry).name != "hooks.json" or "hooks/hooks.json" not in entry, (
                f"{entry} is the conventional path and is loaded automatically; "
                "declare only additional hook files")


def test_the_conventional_hooks_file_still_ships():
    """Removing the manifest entry must not remove the file it pointed at."""
    assert (ROOT / "hooks" / "hooks.json").is_file()
    assert _read("hooks/hooks.json")["hooks"], "the auto-discovered file must have content"
