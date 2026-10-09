"""#150, ADR-015 decision 1: every health check has four outcomes, and only one
of them is a pass.

`pass`, `fail`, `pending` (too early to tell) and `unknown` (the evidence could
not be read). Before this a check was a bool, so "could not read the state
directory" and "the hooks are broken" were the same answer, and "no sub-agent
has run yet" failed a healthy machine for ever.

The status line, the analyst agent and the health page (ADR-017) read this
contract and nothing else, so `--json` and the exit codes are tested as a
contract: exact keys, nothing else on stdout.

Not every check can reach every outcome. Each check is tested for the outcomes
it can reach; the ones it cannot are listed beside it with the reason, rather
than invented to fit.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from stdtel import doctor
from stdtel.doctor import Check

ROOT = Path(__file__).resolve().parent.parent


# --- the result type ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["pass", "fail", "pending", "unknown"])
def test_each_outcome_is_accepted_and_only_pass_is_ok(outcome):
    c = Check("x", outcome, "seen", "do this")
    assert c.outcome == outcome
    assert c.ok is (outcome == "pass")


def test_a_bool_still_means_pass_or_fail():
    """Every existing call site passes a bool; they keep their meaning."""
    assert Check("x", True, "").outcome == "pass"
    assert Check("x", False, "", "fix").outcome == "fail"


def test_an_outcome_outside_the_four_is_refused():
    """A typo must fail loudly, not become a fifth state nobody reads (ADR-005)."""
    with pytest.raises(ValueError):
        Check("x", "passed", "")


def test_observed_at_is_utc_and_recent():
    c = Check("x", True, "")
    at = dt.datetime.fromisoformat(c.observed_at)
    assert at.tzinfo is not None and at.utcoffset() == dt.timedelta(0)
    assert abs((dt.datetime.now(dt.timezone.utc) - at).total_seconds()) < 60


def test_every_outcome_but_pass_carries_a_remedy(monkeypatch, tmp_path):
    """Hermetic: nothing here reads the developer's machine or a live collector."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setattr(doctor, "_content_flags_on", lambda: [])
    for check in doctor.check_all():
        if check.outcome != "pass":
            assert check.remedy, f"{check.name} is {check.outcome} and says nothing to do"


# --- the exit code -----------------------------------------------------------------------------

def _mix(*outcomes):
    return [Check(f"c{i}", o, "", "r") for i, o in enumerate(outcomes)]


@pytest.mark.parametrize("outcomes,code", [
    (("pass",), 0),
    (("pass", "pass"), 0),
    (("pass", "fail"), 1),
    (("fail", "unknown", "pending"), 1),      # a fail wins over anything uncertain
    (("pass", "pending"), 2),
    (("pass", "unknown"), 2),
    (("pending", "unknown"), 2),
    ((), 2),                                  # no checks ran: nothing was shown to be well
])
def test_exit_code_per_outcome_mix(outcomes, code):
    assert doctor.exit_code(_mix(*outcomes)) == code


def _run(monkeypatch, capsys, checks, *argv):
    monkeypatch.setattr(doctor, "check_all", lambda: checks)
    code = doctor.main(list(argv))
    return code, capsys.readouterr().out


@pytest.mark.parametrize("outcomes,code", [(("pass",), 0), (("pass", "fail"), 1), (("pass", "unknown"), 2)])
def test_main_returns_the_exit_code(monkeypatch, capsys, outcomes, code):
    assert _run(monkeypatch, capsys, _mix(*outcomes))[0] == code
    assert _run(monkeypatch, capsys, _mix(*outcomes), "--json")[0] == code


# --- --json ------------------------------------------------------------------------------------

def test_json_is_one_object_per_check_with_exactly_the_contract_keys(monkeypatch, capsys):
    checks = [Check("a", True, "fine"), Check("b", "pending", "not yet", "wait for a sub-agent")]
    _, out = _run(monkeypatch, capsys, checks, "--json")
    got = json.loads(out)                     # stdout is the JSON and nothing else
    assert [set(o) for o in got] == [{"name", "outcome", "observed", "remedy", "observed_at"}] * 2
    assert got[1] == {"name": "b", "outcome": "pending", "observed": "not yet",
                      "remedy": "wait for a sub-agent", "observed_at": checks[1].observed_at}


def test_json_ignores_quiet_so_a_reader_always_sees_every_check(monkeypatch, capsys):
    checks = _mix("pass", "fail")
    _, out = _run(monkeypatch, capsys, checks, "--json", "--quiet")
    assert [o["outcome"] for o in json.loads(out)] == ["pass", "fail"]


def test_json_with_content_check_is_the_probe_alone(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "probe_content", lambda **kw: Check("content dropped", "unknown", "x", "y"))
    monkeypatch.setattr(doctor, "claude_code_endpoint", lambda *a: "http://127.0.0.1:1")
    code = doctor.main(["--json", "--content-check"])
    got = json.loads(capsys.readouterr().out)
    assert [o["name"] for o in got] == ["content dropped"] and code == 2


# --- human output ------------------------------------------------------------------------------

def test_human_output_marks_each_outcome_and_counts_them(monkeypatch, capsys):
    # distinct counts, so a count printed under the wrong outcome shows
    _, out = _run(monkeypatch, capsys, _mix("pass", "fail", "fail", "pending", "pending", "pending",
                                            "unknown", "unknown", "unknown", "unknown"))
    for mark in ("ok", "FAIL", "WAIT", "????"):
        assert f"  {mark}" in out
    assert "1 passed, 2 failed, 3 pending, 4 unknown" in out


def test_quiet_hides_only_passes(monkeypatch, capsys):
    _, out = _run(monkeypatch, capsys, [Check("hidden", True, ""), Check("shown", "pending", "", "r")], "-q")
    assert "shown" in out and "hidden" not in out


# --- a check that raises is unknown, under its own name ----------------------------------------

def test_a_check_that_raises_is_unknown_and_keeps_its_display_name(monkeypatch):
    def collector_ok():
        raise RuntimeError("boom")
    collector_ok.check_name = "collector reachable"
    monkeypatch.setattr(doctor, "CHECKS", (collector_ok,))
    (c,) = doctor.check_all()
    assert (c.name, c.outcome) == ("collector reachable", "unknown")
    assert "boom" in c.detail and c.remedy


def test_every_check_declares_the_name_it_reports_under(monkeypatch):
    """The name is a downstream identifier now (#154, #162): a check must report
    under one name whether it returned or raised."""
    for fn in doctor.CHECKS:
        assert getattr(fn, "check_name", None), fn.__name__


# --- each existing check, per reachable outcome ------------------------------------------------
# A check absent from an outcome below cannot reach it; the reason is beside it.

def _sh(monkeypatch, stdout="", raises=None):
    def run(*a, **kw):
        if raises:
            raise raises
        return subprocess.CompletedProcess(a, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(doctor.subprocess, "run", run)


# hook resolvable: pass / fail / unknown. Never pending: resolution is immediate.
def test_hook_resolvable_pass(monkeypatch):
    _sh(monkeypatch, "/x/stdtel-hook\n")
    assert doctor.hook_resolvable().outcome == "pass"


def test_hook_resolvable_fail(monkeypatch):
    _sh(monkeypatch, "")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    assert doctor.hook_resolvable().outcome == "fail"


def test_hook_resolvable_unknown_when_it_cannot_probe(monkeypatch):
    _sh(monkeypatch, raises=OSError("no sh"))
    c = doctor.hook_resolvable()
    assert c.outcome == "unknown" and c.remedy


# hooks registered: pass / fail. Never pending or unknown: the files are read, or
# their absence is the finding. An unparseable settings.json is a fail, not
# unknown: the broken file is itself the fault, and it breaks Claude Code too.
def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir(exist_ok=True)
    return tmp_path / ".claude"


def test_hooks_registered_pass(monkeypatch, tmp_path):
    (_home(monkeypatch, tmp_path) / "settings.json").write_text(json.dumps({"hooks": {"Stop": "stdtel-hook stop"}}))
    assert doctor.hooks_registered().outcome == "pass"


@pytest.mark.parametrize("body", [None, "{ not json"])
def test_hooks_registered_fail(monkeypatch, tmp_path, body):
    d = _home(monkeypatch, tmp_path)
    if body:
        (d / "settings.json").write_text(body)
    assert doctor.hooks_registered().outcome == "fail"


# plugin in step: pass / fail. The manifest is read leniently; unreadable means
# no plugin, which is a supported install.
def test_plugin_in_step_pass_and_fail(monkeypatch):
    monkeypatch.setattr("stdtel.plugin.installed_plugin", lambda: None)
    assert doctor.plugin_in_step().outcome == "pass"
    monkeypatch.setattr("stdtel.plugin.installed_plugin", lambda: ("m", "0.0.1"))
    monkeypatch.setattr("stdtel.plugin.skew_notice", lambda v: "stale; run: claude plugin update")
    assert doctor.plugin_in_step().outcome == "fail"


# branch identity: pass / fail. git either answers or the join is impossible.
def test_branch_identity_pass_and_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_REPO", "git@github.com:a/b.git")
    monkeypatch.setenv("STDTEL_BRANCH", "feat/x")
    assert doctor.branch_identity().outcome == "pass"
    monkeypatch.delenv("STDTEL_BRANCH")
    monkeypatch.chdir(tmp_path)
    assert doctor.branch_identity().outcome == "fail"


# skill catalogue: pass / fail.
def test_catalogue_pass_and_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(ROOT / "skills"))
    assert doctor.catalogue_ok().outcome == "pass"
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert doctor.catalogue_ok().outcome == "fail"


# scope declarations: pass only. ADR-011 requires nothing of any author, so no
# answer here is a fault.
def test_scope_adoption_only_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("STDTEL_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert doctor.scope_adoption().outcome == "pass"


# collector reachable: pass / fail. A dead collector is the fault itself (spans
# are being dropped now), not unreadable evidence, so it fails.
def test_collector_fail(monkeypatch):
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:1")
    assert doctor.collector_ok().outcome == "fail"


def test_collector_pass(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: None)
    assert doctor.collector_ok().outcome == "pass"


# content dropped: pass / fail / unknown. A leak is a fail; a probe that never
# arrived, or never left, proves nothing either way.
def test_content_probe_outcomes():
    assert doctor.judge_content_probe(tempo=(True, False), loki=(True, False)).outcome == "pass"
    assert doctor.judge_content_probe(tempo=(True, True), loki=(True, False)).outcome == "fail"
    assert doctor.judge_content_probe(tempo=(True, False), loki=(False, False)).outcome == "unknown"


def test_a_probe_that_could_not_be_sent_is_unknown():
    def down(path, body):
        raise ConnectionRefusedError()
    c = doctor.probe_content(send=down, read_tempo=lambda i: i, read_loki=lambda i: i, attempts=1, wait=0)
    assert c.outcome == "unknown" and not c.ok


def test_the_detailed_view_still_refuses_on_an_unknown_probe(monkeypatch, tmp_path):
    """That gate is `if not check.ok`; its safety now rests on unknown being not ok."""
    from stdtel import install
    monkeypatch.setattr("stdtel.doctor.probe_content", lambda **kw: Check("content dropped", "unknown", "x", "y"))
    monkeypatch.setattr("stdtel.doctor.claude_code_endpoint", lambda *a: "http://127.0.0.1:4318")
    target = tmp_path / "settings.json"
    assert install.detailed_view("on", target) == 1
    assert not target.exists() or "OTEL_LOG_TOOL_DETAILS" not in target.read_text()


# hooks running: pass / fail / unknown. Never pending: a session either wrote
# state or did not.
def _state(monkeypatch, tmp_path, owner):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "s1.json").write_text("{}")
    monkeypatch.setenv("STDTEL_STATE_DIR", str(sd))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    if owner:
        p = tmp_path / ".claude" / "projects" / owner
        p.mkdir(parents=True)
        (p / "s1.jsonl").write_text("")


def test_recent_state_pass(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, str(tmp_path.resolve()).replace("/", "-"))
    monkeypatch.chdir(tmp_path.resolve())
    assert doctor.recent_state().outcome == "pass"


def test_recent_state_fail(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, "-elsewhere")
    assert doctor.recent_state().outcome == "fail"


def test_recent_state_whose_project_cannot_be_told_is_unknown_not_pass(monkeypatch, tmp_path):
    """It used to pass. State exists, but whose is unknowable (a Copilot session
    writes no Claude transcript), and an unknowable answer is not a clean one."""
    _state(monkeypatch, tmp_path, None)
    c = doctor.recent_state()
    assert c.outcome == "unknown" and c.remedy


def test_unreadable_state_dir_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_state_files", lambda: (_ for _ in ()).throw(PermissionError("denied")))
    assert doctor.recent_state().outcome == "unknown"
    assert doctor.artefacts_observed().outcome == "unknown"


# artefact events: pass / pending / unknown. Never fail: an event that has not
# fired yet is too early to tell, not a fault. It used to fail a healthy machine
# until someone happened to spawn a sub-agent.
def _events(monkeypatch, tmp_path, observed):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    if observed is not None:
        (tmp_path / "s.json").write_text(json.dumps({"observed_events": observed}))


def test_artefact_events_pass(monkeypatch, tmp_path):
    _events(monkeypatch, tmp_path, ["subagent-start", "subagent-stop", "post-compact"])
    assert doctor.artefacts_observed().outcome == "pass"


@pytest.mark.parametrize("observed", [None, ["subagent-start"]])
def test_artefact_events_not_yet_seen_are_pending(monkeypatch, tmp_path, observed):
    _events(monkeypatch, tmp_path, observed)
    c = doctor.artefacts_observed()
    assert c.outcome == "pending" and c.remedy


def test_the_project_named_as_elsewhere_owns_the_newest_owned_state(monkeypatch, tmp_path):
    """#164 council: the newest file can be unowned; the project named must be the
    owner of the newest file that has one, and the time must be that file's."""
    import os
    sd = tmp_path / "state"
    sd.mkdir()
    for i, sid in enumerate(["old", "mid", "new"]):
        f = sd / f"{sid}.json"
        f.write_text("{}")
        os.utime(f, (1_700_000_000 + i * 3600,) * 2)
    for owner, sid in (("-older-project", "old"), ("-newer-project", "mid")):
        p = tmp_path / ".claude" / "projects" / owner
        p.mkdir(parents=True)
        (p / f"{sid}.jsonl").write_text("")
    monkeypatch.setenv("STDTEL_STATE_DIR", str(sd))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    c = doctor.recent_state()
    assert c.outcome == "fail" and "newer-project" in c.detail and "older-project" not in c.detail
    mid = dt.datetime.fromtimestamp(1_700_000_000 + 3600)
    assert f"{mid:%Y-%m-%d %H:%M}" in c.detail
