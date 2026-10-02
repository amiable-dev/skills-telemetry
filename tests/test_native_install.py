"""ADR-014 decision 2: `stdtel-install` switches on both harnesses' own telemetry,
and neither exports content (#113).

Claude Code's per-request cost and skill attribution exist only as events, so
without these settings #112's loader has nothing to read. The Copilot keys were
read from the VS Code documentation on 2026-09-30 (docs/landscape.md); they are
documented, not yet seen in a live trace (#115).
"""
from __future__ import annotations

import json

import pytest

from stdtel import install

CONTENT_FLAGS = ("OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_RAW_API_BODIES",
                 "OTEL_LOG_ASSISTANT_RESPONSES")


@pytest.fixture
def hook(tmp_path, monkeypatch):
    b = tmp_path / "bin" / "stdtel-hook"
    b.parent.mkdir()
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    monkeypatch.setattr(install, "hook_binary", lambda: b)
    return b


# --- Claude Code ---------------------------------------------------------------------------------

def test_the_native_block_is_exactly_what_the_adr_says(monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    env = install.claude_native_env()
    assert env == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
        "OTEL_METRICS_INCLUDE_REPOSITORY": "true",
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
    }


def test_no_content_flag_and_no_metrics_exporter_is_ever_written():
    """Metrics carry session.id as a label (decision 4); the content flags are
    offered only through stdtel-setup, after the collector proves it drops them."""
    env = install.claude_native_env()
    for key in CONTENT_FLAGS + ("OTEL_METRICS_EXPORTER",):
        assert key not in env


def test_the_endpoint_follows_stdtels_own(monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://collector.internal:4318/v1/traces")
    assert install.claude_native_env()["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.internal:4318"


def test_settings_writes_hooks_and_the_native_block(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    target = tmp_path / "settings.json"
    assert install.main(["settings", "--path", str(target)]) == 0
    written = json.loads(target.read_text())
    assert written["hooks"] and written["env"] == install.claude_native_env()


def test_settings_keeps_a_detailed_view_the_user_already_chose(hook, tmp_path):
    """Consent given through stdtel-setup must survive a reinstall."""
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_LOG_TOOL_DETAILS": "1", "MY_VAR": "x"}}))
    install.main(["settings", "--path", str(target)])
    env = json.loads(target.read_text())["env"]
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1" and env["MY_VAR"] == "x"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_no_native_writes_hooks_only(hook, tmp_path):
    target = tmp_path / "settings.json"
    install.main(["settings", "--path", str(target), "--no-native"])
    assert "env" not in json.loads(target.read_text())


def test_a_metrics_exporter_already_set_is_reported_not_removed(hook, tmp_path, capsys):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_METRICS_EXPORTER": "otlp"}}))
    install.main(["settings", "--path", str(target)])
    assert json.loads(target.read_text())["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
    assert "OTEL_METRICS_EXPORTER" in capsys.readouterr().out


def test_dry_run_shows_the_native_block_too(hook, capsys):
    install.main(["settings", "--dry-run"])
    assert '"CLAUDE_CODE_ENABLE_TELEMETRY"' in capsys.readouterr().out


# --- Copilot -------------------------------------------------------------------------------------

def test_copilot_vscode_settings_never_capture_content(monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    assert install.copilot_vscode_settings() == {
        "github.copilot.chat.otel.enabled": True,
        "github.copilot.chat.otel.exporterType": "otlp-http",
        "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318",
        "github.copilot.chat.otel.captureContent": False,
    }


def test_copilot_cli_env_never_captures_content(monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    assert install.copilot_cli_env() == {
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_ENDPOINT": "http://localhost:4318",
        "COPILOT_OTEL_CAPTURE_CONTENT": "false",
    }


def test_copilot_prints_both_and_needs_no_hook_binary(monkeypatch, capsys):
    """Copilot's native export does not use stdtel-hook, so a missing hook
    binary must not stop it."""
    def missing():
        raise install.InstallError("not installed")
    monkeypatch.setattr(install, "hook_binary", missing)
    assert install.main(["copilot"]) == 0
    out = capsys.readouterr().out
    assert '"github.copilot.chat.otel.captureContent": false' in out
    assert "export COPILOT_OTEL_ENABLED=true" in out


def test_copilot_merges_into_a_vscode_settings_file(tmp_path, capsys):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"editor.fontSize": 13}))
    assert install.main(["copilot", "--vscode-settings", str(target)]) == 0
    got = json.loads(target.read_text())
    assert got["editor.fontSize"] == 13 and got["github.copilot.chat.otel.captureContent"] is False


def test_copilot_refuses_a_settings_file_with_comments(tmp_path, capsys):
    """VS Code settings are JSONC. Rewriting one through json would drop every
    comment, so refuse and print the block instead."""
    target = tmp_path / "settings.json"
    original = '{\n  // my font\n  "editor.fontSize": 13\n}\n'
    target.write_text(original)
    assert install.main(["copilot", "--vscode-settings", str(target)]) == 1
    assert target.read_text() == original
    assert "comments" in capsys.readouterr().err


# --- the detailed view: enabled only through a gate enforced in code (decisions 12, 13) ------------

from stdtel import doctor  # noqa: E402


@pytest.fixture
def probe(monkeypatch):
    result = {"ok": True, "endpoints": []}

    def fake(**kw):
        result["endpoints"].append(kw.get("endpoint"))
        return doctor.Check("content dropped", result["ok"], "dropped" if result["ok"] else "leaked to Loki")
    monkeypatch.setattr(doctor, "probe_content", fake)
    return result


def _env(path):
    return json.loads(path.read_text()).get("env", {}) if path.is_file() else {}


def test_detailed_view_on_after_the_check_passes(tmp_path, probe, monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://localhost:4318")
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"MY_VAR": "x"}}))
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 0
    assert _env(target) == {"MY_VAR": "x", "OTEL_LOG_TOOL_DETAILS": "1"}


def test_detailed_view_refused_when_the_check_fails(tmp_path, probe, monkeypatch, capsys):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    probe["ok"] = False
    target = tmp_path / "settings.json"
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 1
    assert "OTEL_LOG_TOOL_DETAILS" not in _env(target)
    assert "leaked to Loki" in capsys.readouterr().err


def test_a_remote_collector_needs_the_user_to_vouch_for_it(tmp_path, probe, monkeypatch, capsys):
    """The check can only see the stores it can query; a remote collector may
    forward somewhere it cannot (ADR-014, known limitations)."""
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "https://otel.example.com")
    target = tmp_path / "settings.json"
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 1
    assert "OTEL_LOG_TOOL_DETAILS" not in _env(target)
    assert "the collector at https://otel.example.com is not on this machine" in capsys.readouterr().err
    assert install.main(["detailed-view", "on", "--path", str(target), "--collector-confirmed"]) == 0
    assert _env(target)["OTEL_LOG_TOOL_DETAILS"] == "1"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_counts_as_local(host):
    assert install._collector_is_local(f"http://{host}:4318")


def test_a_lookalike_host_is_not_local():
    assert not install._collector_is_local("http://localhost.evil.example:4318")


def test_detailed_view_off_removes_only_the_flag(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_LOG_TOOL_DETAILS": "1", "MY_VAR": "x"}}))
    assert install.main(["detailed-view", "off", "--path", str(target)]) == 0
    assert _env(target) == {"MY_VAR": "x"}


# The flag makes *Claude Code* send content, to the endpoint in *its* settings. The
# gate must judge and probe that one, not stdtel's own, or it proves something
# about a collector Claude Code will never use.

def test_the_gate_judges_the_endpoint_claude_code_will_use(tmp_path, probe, monkeypatch, capsys):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com"}}))
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 1
    assert "the collector at https://otel.example.com is not on this machine" in capsys.readouterr().err
    assert "OTEL_LOG_TOOL_DETAILS" not in _env(target)


def test_the_probe_goes_where_claude_code_will_send(tmp_path, probe, monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "https://otel.example.com")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}}))
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 0
    assert probe["endpoints"] == ["http://127.0.0.1:4318"]


def test_a_project_setting_overrides_the_user_file(tmp_path, probe, monkeypatch, capsys):
    """Claude Code's precedence: project local, then project, then user."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com"}}))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}}))
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 1
    assert "the collector at https://otel.example.com is not on this machine" in capsys.readouterr().err


def test_the_doctors_recheck_probes_claude_codes_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"OTEL_LOG_TOOL_DETAILS": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9999"}}))
    seen = []
    monkeypatch.setattr(doctor, "probe_content",
                        lambda **kw: seen.append(kw.get("endpoint")) or doctor.Check("content dropped", True, "x"))
    doctor.content_dropped()
    assert seen == ["http://127.0.0.1:9999"]


def test_probe_content_sends_to_the_endpoint_it_is_given():
    sent = []
    doctor.probe_content(endpoint="http://127.0.0.1:9999", read_tempo=lambda i: i, read_loki=lambda i: i,
                         attempts=1, wait=0, post=lambda url, body: sent.append(url))
    assert sorted(sent) == ["http://127.0.0.1:9999/v1/logs", "http://127.0.0.1:9999/v1/traces"]


def test_the_skill_names_the_gate_and_never_the_raw_flag():
    """ADR-007: a string check, not a behaviour test. It catches a rename that
    would strand the skill's instructions, and a paste-able bypass."""
    from pathlib import Path
    body = (Path(__file__).resolve().parent.parent / "skills" / "stdtel-setup" / "SKILL.md").read_text()
    for needed in ("stdtel-install detailed-view on", "--collector-confirmed", "--content-check",
                   "stdtel-install settings", "--native-only", "stdtel-install copilot", "captureContent"):
        assert needed in body, needed
    assert '"OTEL_LOG_TOOL_DETAILS": "1"' not in body and "OTEL_LOG_TOOL_DETAILS=1 " not in body


def test_project_local_settings_beat_project_settings(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}}))
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com/"}}))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert doctor.claude_code_endpoint(tmp_path / "user.json") == "https://otel.example.com"


# --- plugin installs: the plugin registers the hooks, so settings must not (#134) -----------------

def test_native_only_writes_the_env_block_and_no_hooks(hook, tmp_path, monkeypatch):
    """With the plugin installed, writing hooks to settings as well makes every
    hook fire twice — which stdtel-doctor flags. A plugin user needs the env alone."""
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
                                  "env": {"OTEL_LOG_TOOL_DETAILS": "1", "MY_VAR": "x"}}))
    assert install.main(["settings", "--path", str(target), "--native-only"]) == 0
    got = json.loads(target.read_text())
    assert got["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}, "hooks untouched"
    assert got["env"] == {"OTEL_LOG_TOOL_DETAILS": "1", "MY_VAR": "x", **install.claude_native_env()}


def test_native_only_needs_no_hook_binary(tmp_path, monkeypatch):
    def missing():
        raise install.InstallError("not installed")
    monkeypatch.setattr(install, "hook_binary", missing)
    target = tmp_path / "settings.json"
    assert install.main(["settings", "--path", str(target), "--native-only"]) == 0
    assert "hooks" not in json.loads(target.read_text())


def test_native_only_dry_run_prints_only_env(tmp_path, capsys):
    target = tmp_path / "settings.json"
    install.main(["settings", "--path", str(target), "--native-only", "--dry-run"])
    assert json.loads(capsys.readouterr().out) == {"env": install.claude_native_env()}
    assert not target.exists()


def test_native_only_and_no_native_contradict(tmp_path, capsys):
    with pytest.raises(SystemExit):
        install.main(["settings", "--path", str(tmp_path / "s.json"), "--native-only", "--no-native"])


# --- #133: never silently redirect someone's telemetry -------------------------------------------

@pytest.mark.parametrize("flag", [[], ["--native-only"]])
def test_an_existing_endpoint_is_kept_and_reported(hook, tmp_path, monkeypatch, capsys, flag):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.team:4317",
                                          "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}}))
    assert install.main(["settings", "--path", str(target), *flag]) == 0
    env = json.loads(target.read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.team:4317"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc", "the protocol belongs to the endpoint it was set with"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    out = capsys.readouterr().out
    assert "kept" in out and "http://collector.team:4317" in out and "--replace-endpoint" in out


def test_replace_endpoint_overwrites_both(hook, tmp_path, monkeypatch):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.team:4317",
                                          "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}}))
    install.main(["settings", "--path", str(target), "--native-only", "--replace-endpoint"])
    env = json.loads(target.read_text())["env"]
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"


def test_the_same_endpoint_is_not_reported_as_kept(hook, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STDTEL_OTLP_ENDPOINT", raising=False)
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318/"}}))
    install.main(["settings", "--path", str(target), "--native-only"])
    assert "kept" not in capsys.readouterr().out


def test_events_go_to_the_logs_endpoint_when_one_is_set(tmp_path, monkeypatch):
    """Claude Code's content-bearing records are events, and a signal-specific
    endpoint beats the generic one wherever either is set."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.example.com/v1/logs"}}))
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}}))
    assert doctor.claude_code_endpoint(user) == "https://logs.example.com"


def test_a_logs_endpoint_in_the_environment_counts_too(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://logs.example.com/v1/logs")
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}}))
    assert doctor.claude_code_endpoint(user) == "https://logs.example.com"


def test_the_gate_refuses_a_remote_logs_endpoint(tmp_path, probe, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
                                          "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.example.com/v1/logs"}}))
    assert install.main(["detailed-view", "on", "--path", str(target)]) == 1
    assert "the collector at https://logs.example.com is not on this machine" in capsys.readouterr().err
