"""ADR-015 decision 4 (#154): the session warns about itself.

A session reads its settings when it starts, so one started before telemetry was
turned on does not export, however long it runs. SessionStart records whether
the variable is in the session's environment, and what the settings resolve to;
the status line says so on every turn, and only when both facts are known.
"""
import json

import pytest

from stdtel import doctor
from stdtel.hooks import cli as hooks
from stdtel.state import SessionState
from stdtel.statusline import render

VAR = "CLAUDE_CODE_ENABLE_TELEMETRY"
WARNING = "usage not exported: started before telemetry was on; restart to fix"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A user settings file and a project with its two, all absent to start."""
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("STDTEL_DISABLED", raising=False)
    monkeypatch.delenv("STDTEL_STATUSLINE", raising=False)
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setattr(doctor.Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    files = {"local": project / ".claude" / "settings.local.json",
             "project": project / ".claude" / "settings.json",
             "user": tmp_path / "home" / ".claude" / "settings.json"}

    def write(level, value=None, raw=None):
        files[level].write_text(raw if raw is not None else json.dumps({"env": {VAR: value}}))
    write.files, write.project = files, project
    return write


def start(home, sid="warn-1", source="startup"):
    hooks.session_start({"session_id": sid, "cwd": str(home.project), "source": source})
    return SessionState.load(sid)


def line(sid="warn-1"):
    return render({"session_id": sid})


# --- the resolution ---------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["local", "project", "user"])
def test_settings_that_enable_telemetry_at_any_level_resolve_enabled(home, level):
    home(level, "1")
    assert doctor.telemetry_setting() == "enabled"


@pytest.mark.parametrize("level", ["local", "project", "user"])
def test_settings_that_disable_telemetry_at_any_level_resolve_opted_out(home, level):
    home(level, "0")
    assert doctor.telemetry_setting() == "opted-out"


def test_no_setting_anywhere_is_opted_out(home):
    assert doctor.telemetry_setting() == "opted-out"


def test_a_higher_level_off_beats_a_lower_level_on(home):
    home("user", "1")
    home("local", "0")
    assert doctor.telemetry_setting() == "opted-out"


def test_an_unreadable_file_above_the_setter_is_unknown(home):
    home("user", "1")
    home("local", raw="{not json")
    assert doctor.telemetry_setting() == "unknown"


def test_an_unreadable_file_below_the_setter_does_not_matter(home):
    home("local", "1")
    home("user", raw="{not json")
    assert doctor.telemetry_setting() == "enabled"


def test_a_file_that_is_not_an_object_is_unknown(home):
    home("project", raw="[1, 2]")
    assert doctor.telemetry_setting() == "unknown"


def test_a_file_without_an_env_block_is_read_not_unknown(home):
    home("local", raw='{"model": "opus"}')
    home("user", "1")
    assert doctor.telemetry_setting() == "enabled"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_values_that_mean_on(home, value):
    home("user", value)
    assert doctor.telemetry_setting() == "enabled"


def test_the_resolver_never_raises(home, monkeypatch):
    monkeypatch.setattr(doctor, "_claude_settings_files", lambda *a, **k: 1 / 0)
    assert doctor.telemetry_setting() == "unknown"


# --- what the session records and shows -------------------------------------------------------

def test_a_session_started_without_the_variable_while_settings_enable_it_warns(home):
    home("user", "1")
    st = start(home)
    assert (st.telemetry_env, st.telemetry_settings) == (False, "enabled")
    assert WARNING in line()


@pytest.mark.parametrize("level", ["local", "project", "user"])
def test_settings_that_disable_telemetry_persist_opted_out_and_show_nothing(home, level):
    home(level, "0")
    assert start(home).telemetry_settings == "opted-out"
    assert WARNING not in line()


def test_an_unreadable_settings_file_persists_unknown_and_shows_nothing(home):
    home("local", raw="{not json")
    assert start(home).telemetry_settings == "unknown"
    assert WARNING not in line()


def test_a_restarted_session_clears_the_warning(home, monkeypatch):
    home("user", "1")
    start(home)
    assert WARNING in line()
    monkeypatch.setenv(VAR, "1")
    st = start(home, source="resume")
    assert st.telemetry_env is True
    assert WARNING not in line()


def test_a_session_with_no_record_shows_nothing(home):
    home("user", "1")
    st = SessionState.load("warn-1")
    st.resource = {"std.branch.hash": "abc"}
    st.save()
    assert SessionState.load("warn-1").telemetry_env is None
    assert WARNING not in line()


def test_a_variable_set_to_off_in_the_environment_is_not_exporting(home, monkeypatch):
    home("user", "1")
    monkeypatch.setenv(VAR, "0")
    assert start(home).telemetry_env is False


def test_the_record_survives_save_and_load(home, monkeypatch):
    monkeypatch.setenv(VAR, "1")
    home("user", "1")
    start(home)
    st = SessionState.load("warn-1")
    assert (st.telemetry_env, st.telemetry_settings) == (True, "enabled")


# --- doctor reports the unreadable file ------------------------------------------------------

def test_doctor_names_an_unreadable_settings_file(home):
    home("project", raw="{not json")
    c = doctor.claude_settings()
    assert c.outcome == "fail"
    assert str(home.files["project"]) in c.detail and c.remedy


def test_doctor_passes_readable_or_absent_settings(home):
    home("user", "1")
    assert doctor.claude_settings().outcome == "pass"


def test_the_settings_check_is_registered(home):
    assert doctor.claude_settings in doctor.CHECKS
    assert doctor.claude_settings.check_name == "claude settings"


# --- mutation survivors ----------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "0", "false", "No", "off", 0])
def test_a_higher_level_set_to_any_off_value_beats_a_lower_on(home, value):
    home("user", "1")
    home("local", value)
    assert doctor.telemetry_setting() == "opted-out"


def test_without_claude_project_dir_the_hook_resolves_from_the_payload_cwd(home, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    monkeypatch.chdir(tmp_path)
    home("local", "1")
    assert start(home).telemetry_settings == "enabled"


def test_a_restart_without_the_variable_warns_again(home, monkeypatch):
    home("user", "1")
    monkeypatch.setenv(VAR, "1")
    start(home)
    monkeypatch.delenv(VAR)
    assert start(home, source="resume").telemetry_env is False
    assert WARNING in line()


def test_settings_recorded_without_the_environment_fact_show_nothing(home):
    st = SessionState.load("warn-1")
    st.resource, st.telemetry_settings = {"std.branch.hash": "abc"}, "enabled"
    st.save()
    assert WARNING not in line()
