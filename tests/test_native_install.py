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
