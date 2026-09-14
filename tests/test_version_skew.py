"""The plugin and the package are two installs, and neither updates the other.

The plugin ships hook registration, the launcher, the skills and the agent. The
package ships everything that runs. A developer updates one and assumes the
other followed — and the failure is quiet in both directions: a missing package
means the launcher exits 0 in silence, and a stale plugin means stale skills with
no symptom at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stdtel.plugin import installed_plugin, skew_notice


def _plugin(home: Path, marketplace: str, version: str, name: str = "stdtel") -> Path:
    d = home / ".claude" / "plugins" / "marketplaces" / marketplace / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    return d / "plugin.json"


def test_no_plugin_is_not_a_fault(tmp_path, monkeypatch):
    """Registering the hooks in settings.json is a supported install. The plugin
    is a convenience, so its absence must not be reported as breakage."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert installed_plugin() is None
    assert skew_notice("0.3.1") == ""


def test_matching_versions_say_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _plugin(tmp_path, "amiable-standards", "0.3.1")
    assert installed_plugin() == ("amiable-standards", "0.3.1")
    assert skew_notice("0.3.1") == ""


def test_a_stale_plugin_is_named_with_the_command_that_fixes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _plugin(tmp_path, "amiable-standards", "0.2.3")
    notice = skew_notice("0.3.1")
    assert "0.2.3" in notice and "0.3.1" in notice
    assert "claude plugin update stdtel@amiable-standards" in notice


def test_a_plugin_ahead_of_the_package_is_reported_too(tmp_path, monkeypatch):
    """The likelier direction once the plugin auto-updates: the marketplace moved
    and `uv tool install` did not."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _plugin(tmp_path, "amiable-standards", "0.4.0")
    notice = skew_notice("0.3.1")
    assert "uv tool install stdtel" in notice


def test_another_marketplaces_plugin_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _plugin(tmp_path, "someone-else", "9.9.9", name="not-stdtel")
    assert installed_plugin() is None


def test_an_unreadable_plugin_manifest_is_not_an_error(tmp_path, monkeypatch):
    """A diagnostic must never be the thing that breaks."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = _plugin(tmp_path, "amiable-standards", "0.3.1")
    p.write_text("{ not json")
    assert installed_plugin() is None
    assert skew_notice("0.3.1") == ""


# --- the doctor check ---

def test_doctor_reports_the_skew(tmp_path, monkeypatch):
    from stdtel.doctor import plugin_in_step
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _plugin(tmp_path, "amiable-standards", "0.2.3")
    check = plugin_in_step()
    assert not check.ok and "0.2.3" in check.detail and check.remedy


def test_doctor_passes_when_there_is_no_plugin(tmp_path, monkeypatch):
    from stdtel.doctor import plugin_in_step
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    check = plugin_in_step()
    assert check.ok, "hooks registered in settings.json is a supported install"


def test_doctor_is_wired_in():
    from stdtel.doctor import CHECKS, plugin_in_step
    assert plugin_in_step in CHECKS


# --- the plugin has to be able to say so when the package is missing ---

import os          # noqa: E402
import subprocess  # noqa: E402

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "stdtel-hook"


def _launch(event: str):
    return subprocess.run(["sh", str(LAUNCHER), event], input="{}", text=True,
                          capture_output=True,
                          env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})


def test_a_missing_package_is_announced_once_per_session():
    """The circularity this closes: with the package absent, `stdtel-doctor`
    does not exist either, so the only thing that can report the problem is the
    plugin — and the plugin's launcher is the only part of it that runs.

    SessionStart output reaches the developer as session context, so this is the
    one place the notice can land.
    """
    r = _launch("session-start")
    assert r.returncode == 0, "a telemetry problem must still never block"
    assert "uv tool install stdtel" in r.stdout


def test_every_other_event_stays_silent():
    """PreToolUse and PostToolUse fire on every tool call. A notice there is not
    information, it is noise on every keystroke — which is how people learn to
    ignore it."""
    for event in ("pre-tool-use", "post-tool-use", "post-tool-use-failure", "stop"):
        r = _launch(event)
        assert r.returncode == 0
        assert r.stdout == "" and r.stderr == "", f"{event} must be silent"


def test_session_start_says_so_once(tmp_path, monkeypatch, capsys):
    """SessionStart is the only hook whose output a developer reads, and it fires
    once. PreToolUse fires on every Skill call and Stop on every turn."""
    from stdtel.hooks.cli import session_start
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    _plugin(tmp_path, "amiable-standards", "0.0.1")
    session_start({"session_id": "s1", "cwd": str(tmp_path)})
    assert "out of step" in capsys.readouterr().out


def test_session_start_is_quiet_when_they_agree(tmp_path, monkeypatch, capsys):
    import stdtel
    from stdtel.hooks.cli import session_start
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    _plugin(tmp_path, "amiable-standards", stdtel.__version__)
    session_start({"session_id": "s2", "cwd": str(tmp_path)})
    assert capsys.readouterr().out == ""
