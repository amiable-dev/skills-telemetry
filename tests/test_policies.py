"""The OPA policies behind the project's primary metric (first-time policy pass rate).

Until `policies/` existed, grade() only ran under --dry-run, which returns True for
every policy — so the headline number was a stub. These tests pin the discriminating
behaviour: the with-skill and without-skill arms must actually grade differently.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from eval.run_eval import build_input, grade

POLICIES = Path(__file__).resolve().parent.parent / "policies"
BOTH = ["logging.required_fields", "logging.no_pii"]

pytestmark = pytest.mark.skipif(shutil.which("opa") is None, reason="opa not installed")

COMPLIANT = (
    "import logging\n"
    "logger = logging.getLogger(__name__)\n"
    "logger.info('request', extra={'timestamp': t, 'level': lvl, 'service': svc,\n"
    "                             'trace_id': tid, 'span_id': sid, 'message': msg})\n"
)


def write(tmp_path: Path, content: str, name: str = "app/main.py") -> Path:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return tmp_path


def test_policies_are_valid_rego():
    assert subprocess.run(["opa", "check", str(POLICIES)]).returncode == 0


def test_compliant_code_passes_both(tmp_path):
    assert grade(write(tmp_path, COMPLIANT), BOTH, POLICIES, dry_run=False) == {
        "logging.required_fields": True, "logging.no_pii": True}


def test_no_logging_at_all_fails(tmp_path):
    """A no-op solution must not pass by vacuous truth — this is the without-skill arm."""
    r = grade(write(tmp_path, "from fastapi import FastAPI\napp = FastAPI()\n"), BOTH, POLICIES, dry_run=False)
    assert r["logging.required_fields"] is False


def test_missing_required_field_fails(tmp_path):
    partial = COMPLIANT.replace("'span_id': sid, ", "")
    r = grade(write(tmp_path, partial), BOTH, POLICIES, dry_run=False)
    assert r["logging.required_fields"] is False


def test_logged_credential_fails_no_pii(tmp_path):
    leaky = COMPLIANT.replace("'message': msg}", "'message': msg, 'password': pw}")
    r = grade(write(tmp_path, leaky), BOTH, POLICIES, dry_run=False)
    assert r["logging.no_pii"] is False
    assert r["logging.required_fields"] is True      # independent policies


def test_logged_request_body_fails_no_pii(tmp_path):
    body = COMPLIANT.replace("'message': msg}", "'message': request.body}")
    assert grade(write(tmp_path, body), BOTH, POLICIES, dry_run=False)["logging.no_pii"] is False


def test_unevaluable_policy_fails_loudly_not_silently(tmp_path, capsys):
    """An unknown policy id must fail and say so — never pass by silence.

    The old grade() swallowed every exception into a bare `except`, which turned
    a broken grader into a clean sweep of passes.
    """
    r = grade(write(tmp_path, COMPLIANT), ["logging.does_not_exist"], POLICIES, dry_run=False)
    assert r == {"logging.does_not_exist": False}
    assert "cannot read opa output" in capsys.readouterr().err


def test_missing_policy_root_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="does not exist"):
        grade(tmp_path, BOTH, tmp_path / "absent", dry_run=False)


# --- the input document ---

def test_build_input_collects_source_and_skips_noise(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n")
    (tmp_path / "app" / "logo.png").write_bytes(b"\x89PNG\r\n")
    doc = build_input(tmp_path)
    assert [f["path"] for f in doc["files"]] == ["app/main.py"]
    assert doc["files"][0]["content"] == "x = 1\n"


def test_the_fixture_the_task_references_exists():
    fixture = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "fastapi-min"
    assert fixture.is_dir(), "eval/tasks.yaml references this fixture"
    assert build_input(fixture)["files"], "fixture must contain gradeable source"


def test_bare_fixture_fails_the_metric():
    """The starting state must fail, or with-vs-without measures nothing."""
    fixture = Path(__file__).resolve().parent.parent / "eval" / "fixtures" / "fastapi-min"
    assert grade(fixture, BOTH, POLICIES, dry_run=False)["logging.required_fields"] is False


def test_multiline_log_call_is_graded_whole(tmp_path):
    """The bug this policy was rewritten for: a leak on a later line of the call."""
    leaky = ("import logging\n"
             "logger = logging.getLogger(__name__)\n"
             "logger.info(\n"
             "    'request',\n"
             "    extra={'timestamp': t, 'level': l, 'service': s,\n"
             "           'trace_id': tr, 'span_id': sp, 'message': m,\n"
             "           'api_key': key},\n"
             ")\n")
    r = grade(write(tmp_path, leaky), BOTH, POLICIES, dry_run=False)
    assert r["logging.no_pii"] is False
    assert r["logging.required_fields"] is True


def test_sensitive_word_outside_a_log_call_is_not_flagged(tmp_path):
    """Precision: handling a password is fine, logging one is not."""
    ok = "password = get_secret()\nvalidate(password)\n" + COMPLIANT
    assert grade(write(tmp_path, ok), BOTH, POLICIES, dry_run=False)["logging.no_pii"] is True


# --- the manifest contract is itself a policy, so onboarding is measurable ---

MANIFEST = ["telemetry.manifest_valid"]


def test_our_own_catalogue_satisfies_its_own_policy():
    skills = Path(__file__).resolve().parent.parent / "skills"
    assert grade(skills, MANIFEST, POLICIES, dry_run=False) == {"telemetry.manifest_valid": True}


def test_manifest_policy_actually_sees_skill_files():
    """It passed vacuously until .md was gradeable — the same trap as required_fields."""
    skills = Path(__file__).resolve().parent.parent / "skills"
    paths = [f["path"] for f in build_input(skills)["files"]]
    assert any(p.endswith("SKILL.md") for p in paths), paths


def test_contract_fields_at_top_level_are_rejected(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\nversion: 1.0.0\nstandard_id: STD-X-001\n---\nbody\n")
    assert grade(tmp_path, MANIFEST, POLICIES, dry_run=False)["telemetry.manifest_valid"] is False


def test_missing_front_matter_is_rejected(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# just a heading\n")
    assert grade(tmp_path, MANIFEST, POLICIES, dry_run=False)["telemetry.manifest_valid"] is False
