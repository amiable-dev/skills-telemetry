"""`stdtel doctor`: make a silently-broken install visible.

Every failure mode in this system is quiet by construction — hooks exit 0 so
telemetry never blocks the developer, which means a broken install looks exactly
like a working one. Four install-time bugs in this project's history were found
by a person using it, none by its tests.

Each check returns a verdict *and* the remedy: "what do I do about it" is the
part a diagnostic usually omits.
"""
import os
from pathlib import Path

import pytest

from stdtel.doctor import Check, check_all, hook_resolvable, ticket_key, catalogue_ok, collector_ok

ROOT = Path(__file__).resolve().parent.parent


# --- the shape of a check ---

def test_a_check_carries_a_remedy_not_just_a_verdict():
    for check in check_all():
        assert check.name and isinstance(check.ok, bool)
        if not check.ok:
            assert check.remedy, f"{check.name}: a failing check must say what to do"


def test_checks_never_raise_even_with_nothing_configured(monkeypatch, tmp_path):
    """Running it on a broken machine is the whole point; it must not itself break."""
    for var in ("STDTEL_OTLP_ENDPOINT", "STDTEL_SKILLS_ROOT", "STDTEL_STATE_DIR", "STDTEL_BRANCH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    for check in check_all():
        assert isinstance(check.ok, bool)


# --- the #13 case: the hook must resolve the way a hook resolves it ---

def test_hook_resolvable_uses_a_hook_like_environment():
    check = hook_resolvable()
    assert "sh -c" in check.detail or "PATH" in check.detail or check.ok


def test_hook_not_resolvable_is_reported_with_the_install_command(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr("shutil.which", lambda _: None)
    check = hook_resolvable()
    assert not check.ok
    assert "uv tool install" in check.remedy or "stdtel-install" in check.remedy


# --- attribution faults, which are only fixable while the work happens ---

def test_ticket_key_flags_a_branch_without_one(monkeypatch):
    monkeypatch.setenv("STDTEL_BRANCH", "fix/some-thing")
    check = ticket_key()
    assert not check.ok
    assert "unattributed" in check.detail
    assert "branch" in check.remedy.lower()


def test_ticket_key_passes_on_a_prefixed_branch(monkeypatch):
    monkeypatch.setenv("STDTEL_BRANCH", "feature/STDTEL-15-doctor")
    check = ticket_key()
    assert check.ok and "STDTEL-15" in check.detail


# --- catalogue and collector ---

def test_catalogue_reports_how_many_skills_it_found(monkeypatch):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(ROOT / "skills"))
    check = catalogue_ok()
    assert check.ok and any(ch.isdigit() for ch in check.detail)


def test_empty_catalogue_is_a_failure_with_a_remedy(monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    check = catalogue_ok()
    assert not check.ok
    assert "unversioned" in check.remedy or "STDTEL_SKILLS_ROOT" in check.remedy


def test_unreachable_collector_says_spans_are_being_dropped(monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:1")
    check = collector_ok()
    assert not check.ok
    assert "drop" in check.remedy.lower() or "drop" in check.detail.lower()


def test_collector_check_is_bounded(monkeypatch):
    """A diagnostic that hangs on a dead endpoint is not a diagnostic."""
    import time
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://10.255.255.1:4318")
    t0 = time.monotonic()
    collector_ok()
    assert time.monotonic() - t0 < 6, "the collector check must time out quickly"


def test_ticket_key_falls_back_to_git_when_no_env_is_set(monkeypatch):
    """The env-var path was the only one covered, so a missing `_git_branch`
    helper shipped and only showed when the tool was actually run."""
    monkeypatch.delenv("STDTEL_BRANCH", raising=False)
    check = ticket_key()                          # must not raise
    assert isinstance(check.ok, bool) and check.detail


def test_every_check_runs_without_an_exception_escaping():
    """check_all() traps exceptions, which would hide a broken check behind a
    generic failure — assert each one individually instead."""
    from stdtel.doctor import CHECKS
    for fn in CHECKS:
        result = fn()
        assert isinstance(result, Check), fn.__name__
        assert "check itself failed" not in result.detail, f"{fn.__name__}: {result.detail}"


# --- the false positive this check produced on a real machine ---

def test_unrelated_hooks_are_not_mistaken_for_ours(tmp_path, monkeypatch):
    """It reported "registered in settings AND as a plugin" on a machine whose
    settings held only iterm2 status hooks.

    The check tested `bool(settings["hooks"])` — any hooks at all — so any user
    with their own hooks got a confidently wrong double-registration warning.
    """
    import json
    from stdtel.doctor import hooks_registered
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {
        "Notification": [{"hooks": [{"type": "command", "command": "/usr/local/bin/cc-status"}]}]}}))
    check = hooks_registered()
    assert "AND as a plugin" not in check.detail, check.detail
    assert not check.ok, "no stdtel hooks anywhere means nothing is recorded"


def test_stdtel_hooks_in_settings_are_recognised(tmp_path, monkeypatch):
    import json
    from stdtel.doctor import hooks_registered
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "/x/bin/stdtel-hook stop"}]}]}}))
    assert hooks_registered().ok


def test_double_registration_is_still_caught(tmp_path, monkeypatch):
    import json
    from stdtel.doctor import hooks_registered
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "stdtel-hook stop"}]}]}}))
    (claude / "plugins" / "installed_plugins.json").write_text('{"stdtel@amiable-standards": true}')
    check = hooks_registered()
    assert not check.ok and "AND as a plugin" in check.detail


def test_plugin_only_registration_reports_whether_the_cli_is_installed(tmp_path, monkeypatch):
    """The case that produced no data and no error: plugin enabled, package absent."""
    import json
    from stdtel.doctor import hooks_registered
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text('{"stdtel@amiable-standards": true}')
    check = hooks_registered()
    assert check.ok and "plugin" in check.detail


# --- #44: "hooks running" was true, and about somebody else's project ---

def _session(state_dir: Path, projects: Path, session_id: str, project: str | None):
    """A session state file, optionally with the transcript that places it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{session_id}.json").write_text("{}")
    if project:
        (projects / project).mkdir(parents=True, exist_ok=True)
        (projects / project / f"{session_id}.jsonl").write_text("")


def _recent_state(monkeypatch, tmp_path, cwd: Path, sessions: list[tuple[str, str | None]]):
    from stdtel.doctor import recent_state
    state_dir, projects = tmp_path / "state", tmp_path / ".claude" / "projects"
    monkeypatch.setenv("STDTEL_STATE_DIR", str(state_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)
    for session_id, project in sessions:
        _session(state_dir, projects, session_id, project)
    return recent_state()


def test_state_from_this_project_passes(monkeypatch, tmp_path):
    cwd = tmp_path / "work" / "mine"
    check = _recent_state(monkeypatch, tmp_path, cwd, [("sess-1", str(cwd).replace("/", "-"))])
    assert check.ok and "this project" in check.detail


def test_state_only_from_another_project_is_not_ok(monkeypatch, tmp_path):
    """The #44 case exactly: every state file belonged to a different project,
    the session being watched had none, and the report said `ok hooks running`.

    Claude Code reads hook configuration at session start, so a session that was
    already open when stdtel was installed never runs the hooks — for its whole
    life, however long that is.
    """
    cwd = tmp_path / "work" / "mine"
    check = _recent_state(monkeypatch, tmp_path, cwd, [("sess-2", "-somewhere-else")])
    assert not check.ok
    assert "somewhere-else" in check.detail, "say whose data it is, not just that it exists"
    assert "restart" in check.remedy.lower()


def test_no_state_at_all_is_distinct_from_state_elsewhere(monkeypatch, tmp_path):
    cwd = tmp_path / "work" / "mine"
    empty = _recent_state(monkeypatch, tmp_path, cwd, [])
    elsewhere = _recent_state(monkeypatch, tmp_path, cwd, [("sess-3", "-elsewhere")])
    assert not empty.ok and not elsewhere.ok
    assert empty.detail != elsewhere.detail, "the two need different remedies"


def test_state_whose_project_cannot_be_told_is_not_reported_as_this_project(monkeypatch, tmp_path):
    """Copilot writes no Claude transcript, so ownership is sometimes unknowable.

    Unknown is not the same as wrong: it passes, but it must not claim the state
    came from here.
    """
    cwd = tmp_path / "work" / "mine"
    check = _recent_state(monkeypatch, tmp_path, cwd, [("sess-4", None)])
    assert check.ok
    assert "this project" not in check.detail


def test_the_installer_says_open_sessions_must_be_restarted(capsys, tmp_path, monkeypatch):
    """The moment a developer needs to hear it is the moment they install.

    Without it, they install, keep working in the session they already had open,
    see nothing for hours, and conclude the tool is broken (#44).
    """
    from stdtel import install
    monkeypatch.setattr(install, "hook_binary", lambda: "/somewhere/stdtel-hook")
    settings = tmp_path / "settings.json"
    assert install.main(["settings", "--path", str(settings)]) == 0
    out = capsys.readouterr().out.lower()
    assert "restart" in out and "already open" in out
