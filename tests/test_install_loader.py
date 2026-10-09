"""#171, ADR-015 decision 5: `stdtel-install loader` schedules `stdtel-load`.

On macOS a launchd agent with `StartInterval`, which coalesces intervals missed
during sleep into one run on wake; on Linux a systemd user timer with
`Persistent=true`. Neither inherits a shell profile, so the definition carries
the absolute path, the STDTEL_* settings and a PATH that reaches `gh` and `git`
(ADR-003's scar, again). It holds the DSN, so it is mode 0600 and never printed.

Nothing here touches this machine's scheduler: the activation commands are
injected, and every file is written under a temporary home.
"""
from __future__ import annotations

import plistlib
import stat

import pytest

from stdtel import scheduler as sch

BIN = "/opt/tools/stdtel/bin/stdtel-load"
SECRET = "postgresql://postgres:s3cret-pw@db:5432/stdtel"


@pytest.fixture
def env(monkeypatch):
    for k in ("STDTEL_DSN", "STDTEL_TEMPO", "STDTEL_LOKI", "STDTEL_LOAD_REPO", "STDTEL_LOAD_INTERVAL", "STDTEL_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STDTEL_DSN", SECRET)
    monkeypatch.setenv("STDTEL_LOAD_REPO", "amiable-dev/skills-telemetry")
    monkeypatch.setattr(sch, "_which", lambda name: {"gh": "/opt/homebrew/bin/gh", "git": "/usr/bin/git"}.get(name))


# --- what the job carries --------------------------------------------------------------------

def test_the_job_environment_carries_the_settings_that_are_set_and_a_path(env):
    e = sch.job_env()
    assert e["STDTEL_DSN"] == SECRET and e["STDTEL_LOAD_REPO"] == "amiable-dev/skills-telemetry"
    assert "STDTEL_TEMPO" not in e, "an unset setting is left to stdtel-load's own default"
    assert e["PATH"].split(":")[:1] == ["/opt/homebrew/bin"] and "/usr/bin" in e["PATH"].split(":")


# --- launchd ---------------------------------------------------------------------------------

def test_the_launchd_agent(env, tmp_path):
    p = plistlib.loads(sch.render_launchd(BIN, sch.job_env(), 900, tmp_path / "loader.log"))
    assert p["Label"] == sch.LABEL and p["ProgramArguments"] == [BIN]
    assert p["StartInterval"] == 900 and p["RunAtLoad"] is True
    assert p["EnvironmentVariables"]["STDTEL_DSN"] == SECRET
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "loader.log")


# --- systemd ---------------------------------------------------------------------------------

def test_the_systemd_service_and_timer(env):
    service, timer = sch.render_systemd(BIN, sch.job_env(), 900)
    assert f"ExecStart={BIN}" in service and "Type=oneshot" in service
    assert f'Environment="STDTEL_DSN={SECRET}"' in service
    assert "OnCalendar=*:0/15" in timer and "Persistent=true" in timer
    assert f"Unit={sch.UNIT}.service" in timer


@pytest.mark.parametrize("seconds,cal", [(900, "*:0/15"), (60, "*:0/1"), (90, "*:0/1"), (3600, "*-*-* 0/1:00:00"),
                                         (7200, "*-*-* 0/2:00:00"), (30, "*:0/1")])
def test_the_interval_becomes_a_calendar_step(seconds, cal):
    """Persistent= only applies to OnCalendar timers, so the interval is a calendar
    step: whole minutes under an hour, whole hours above."""
    assert sch.on_calendar(seconds) == cal


def test_a_value_with_a_quote_or_newline_cannot_break_out_of_the_unit():
    with pytest.raises(ValueError):
        sch.render_systemd(BIN, {"STDTEL_DSN": 'x"\nExecStartPre=/bin/evil'}, 900)


# --- the round trip ---------------------------------------------------------------------------

class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return 0


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_reinstall_uninstall(env, tmp_path, platform, capsys):
    run = Runner()
    assert sch.install(platform=platform, home=tmp_path, binary=BIN, interval=900, run=run) == 0
    files = sch.paths(platform, tmp_path)
    assert all(f.exists() for f in files)
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files), "it holds the DSN"
    activated = len(run.calls)
    assert activated >= 1

    assert sch.install(platform=platform, home=tmp_path, binary=BIN, interval=900, run=run) == 0
    assert len(run.calls) == activated, "installing twice is a no-op"
    assert "already installed" in capsys.readouterr().out

    assert sch.install(platform=platform, home=tmp_path, binary=BIN, interval=600, run=run) == 0
    assert len(run.calls) > activated, "a changed definition is rewritten and reloaded"

    assert sch.uninstall(platform=platform, home=tmp_path, run=run) == 0
    assert not any(f.exists() for f in files)
    assert sch.uninstall(platform=platform, home=tmp_path, run=run) == 0, "uninstalling twice is a no-op"


@pytest.mark.parametrize("platform,first", [("darwin", "launchctl"), ("linux", "systemctl")])
def test_activation_uses_the_platforms_own_command(env, tmp_path, platform, first):
    run = Runner()
    sch.install(platform=platform, home=tmp_path, binary=BIN, interval=900, run=run)
    assert all(c[0] == first for c in run.calls)
    if platform == "linux":
        assert ["systemctl", "--user", "enable", "--now", f"{sch.UNIT}.timer"] in run.calls
    else:
        assert any(c[1] == "bootstrap" for c in run.calls)


def test_the_dsn_is_never_printed(env, tmp_path, capsys):
    for platform in ("darwin", "linux"):
        sch.install(platform=platform, home=tmp_path, binary=BIN, interval=900, run=Runner())
        sch.uninstall(platform=platform, home=tmp_path, run=Runner())
    out = capsys.readouterr()
    assert "s3cret-pw" not in out.out + out.err


def test_a_failing_activation_is_reported_and_fails(env, tmp_path, capsys):
    assert sch.install(platform="linux", home=tmp_path, binary=BIN, interval=900, run=lambda argv: 1) == 1
    assert "systemctl" in capsys.readouterr().err


def test_an_unsupported_platform_is_refused(env, tmp_path, capsys):
    assert sch.install(platform="win32", home=tmp_path, binary=BIN, interval=900, run=Runner()) == 1
    assert "win32" in capsys.readouterr().err


# --- preconditions ----------------------------------------------------------------------------

def test_without_the_warehouse_extras_it_refuses(env, tmp_path, monkeypatch, capsys):
    """Never schedule a job that would fail every interval."""
    monkeypatch.setattr("stdtel.load._extras_missing", lambda: ["psycopg"])
    from stdtel import install
    run = Runner()
    monkeypatch.setattr(sch, "_runner", lambda: run)
    assert install.main(["loader", "--home", str(tmp_path)]) == 1
    assert "stdtel[warehouse]" in capsys.readouterr().err and run.calls == []
    assert not any(f.exists() for f in sch.paths("darwin", tmp_path) + sch.paths("linux", tmp_path))


def test_the_binary_is_the_one_beside_this_installation(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "stdtel-load").write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.executable", str(fake / "python"))
    assert sch.load_binary() == str(fake / "stdtel-load")


def test_the_cli_installs_and_uninstalls(env, tmp_path, monkeypatch):
    from stdtel import install
    run = Runner()
    monkeypatch.setattr(sch, "_runner", lambda: run)
    monkeypatch.setattr("stdtel.load._extras_missing", lambda: [])
    monkeypatch.setattr(sch, "load_binary", lambda: BIN)
    assert install.main(["loader", "--home", str(tmp_path)]) == 0
    assert any(f.exists() for f in sch.paths(sch.current_platform(), tmp_path))
    assert install.main(["loader", "--uninstall", "--home", str(tmp_path)]) == 0
    assert not any(f.exists() for f in sch.paths(sch.current_platform(), tmp_path))


# --- the liveness remedy now has a command ---------------------------------------------------

def test_loader_liveness_points_at_the_scheduler():
    from stdtel import warehouse_health as wh
    import datetime as dt
    now = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)
    state = {n: wh.LoaderState(None, None, False) for n in wh.LOADERS}
    assert "stdtel-install loader" in wh.judge_liveness(state, now=now, interval_s=900).remedy


def test_a_definition_that_was_world_readable_is_tightened(env, tmp_path):
    for platform in ("darwin", "linux"):
        for f in sch.paths(platform, tmp_path):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("old")
            f.chmod(0o644)
        sch.install(platform=platform, home=tmp_path, binary=BIN, interval=900, run=Runner())
        assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in sch.paths(platform, tmp_path))
