"""#151, ADR-015 decision 2: `native coverage` names the sessions Claude Code is
not exporting for.

A session reads its telemetry settings once, at start. One started before
native telemetry was switched on runs its whole life without exporting, and a
resume is not a start. On 2026-10-09 eight such sessions had run for a week
while the dashboard showed blank panels and nothing said why.

Correlated on the session id: stdtel's state file is named by the hook
payload's `session_id`, which equals Claude Code's `session.id` on its events.
"""
from __future__ import annotations

import json
import time

import pytest

from stdtel import coverage, doctor
from stdtel.coverage import Session

NOW = 1_800_000_000.0
DAY = 86400.0


def s(sid, first_turn_ago=3600.0, written_ago=60.0, harness="claude-code", repo="a/b", started_ago=7200.0,
      last_turn_ago=None):
    ago = lambda v: None if v is None else NOW - v          # noqa: E731
    return Session(session_id=sid, harness=harness, repo=repo, started_at=NOW - started_ago,
                   first_turn_at=ago(first_turn_ago), written_at=NOW - written_ago,
                   last_turn_at=ago(last_turn_ago))


def judge(sessions, observed=frozenset(), retention_s=30 * DAY):
    return coverage.judge(sessions, observed=set(observed), retention_s=retention_s, now=NOW)


# --- the six W2 cases ------------------------------------------------------------------------

def test_observed():
    c = judge([s("x")], observed={"x"})
    assert c.outcome == "pass" and "1 of 1" in c.detail


def test_missing_past_the_grace_period_is_named_with_project_and_start():
    c = judge([s("dead-1", repo="amiable-dev/docusaurus"), s("ok")], observed={"ok"})
    assert c.outcome == "fail"
    assert "dead-1" in c.detail and "amiable-dev/docusaurus" in c.detail
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 7200)) in c.detail
    assert c.remedy.startswith("restart it; resuming is not enough")


def test_pending_inside_the_grace_period():
    c = judge([s("new", first_turn_ago=300)])
    assert c.outcome == "pending" and c.remedy


def test_grace_is_ten_minutes():
    assert coverage.GRACE_S == 600
    assert judge([s("edge", first_turn_ago=599)]).outcome == "pending"
    assert judge([s("edge", first_turn_ago=601)]).outcome == "fail"


def test_unknown_with_loki_down():
    def down():
        raise ConnectionRefusedError("nothing on 11010")
    c = coverage.native_coverage(states=lambda: [s("x")], observed=lambda ids, since: down(),
                                 retention=lambda: 30 * DAY, now=NOW)
    assert c.outcome == "unknown" and "Loki" in c.detail and c.remedy


def test_a_session_with_no_completed_turn_is_excluded():
    c = judge([s("idle", first_turn_ago=None), s("x")], observed={"x"})
    assert c.outcome == "pass" and "idle" not in c.detail and "1 of 1" in c.detail


def test_retention_shorter_than_the_window_is_unknown_not_missing():
    """Judged per session (#166 round 3): a session whose turns all lie beyond what
    Loki keeps is unknown; one with a turn inside it is still judged."""
    c = judge([s("aged", first_turn_ago=20 * 3600)], retention_s=12 * 3600)
    assert c.outcome == "unknown" and "aged" in c.detail and "12 h" in c.detail
    assert judge([s("recent", first_turn_ago=3600)], retention_s=12 * 3600).outcome == "fail"


# --- the population ---------------------------------------------------------------------------

def test_copilot_sessions_are_not_judged():
    """A Copilot session can never have a claude-code api_request."""
    c = judge([s("cp", harness="copilot"), s("x")], observed={"x"})
    assert c.outcome == "pass" and "cp" not in c.detail


def test_state_older_than_the_window_is_not_judged():
    c = judge([s("old", written_ago=DAY + 60), s("x")], observed={"x"})
    assert c.outcome == "pass" and "old" not in c.detail


def test_no_session_to_judge_is_pending_not_a_pass():
    """ADR-005: vacuous truth is a bug."""
    c = judge([s("idle", first_turn_ago=None)])
    assert c.outcome == "pending" and c.remedy


def test_a_session_whose_first_turn_predates_retention_is_unknown():
    """Its records may have aged out, so their absence proves nothing."""
    c = judge([s("ancient", first_turn_ago=40 * DAY, started_ago=40 * DAY)], retention_s=30 * DAY)
    assert c.outcome == "unknown" and "ancient" in c.detail


def test_precedence_missing_over_pending_over_unknown():
    many = [s("dead"), s("new", first_turn_ago=60), s("ancient", first_turn_ago=40 * DAY)]
    assert judge(many).outcome == "fail"
    assert judge(many[1:]).outcome == "pending"
    assert judge(many[2:]).outcome == "unknown"


def test_an_observed_session_inside_the_grace_period_counts_as_observed():
    assert judge([s("fast", first_turn_ago=30)], observed={"fast"}).outcome == "pass"


# --- reading state ----------------------------------------------------------------------------

def test_sessions_are_read_from_state_files(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (tmp_path / "abc.json").write_text(json.dumps({
        "resource": {"std.harness": "claude-code", "std.repo": "a/b"}, "started_at": 10.0,
        "first_turn_at": 20.0}))
    (tmp_path / "torn.json").write_text("{ half")
    os.utime(tmp_path / "abc.json", (30.0, 30.0))
    (got,) = coverage.read_states()
    assert got == Session("abc", "claude-code", "a/b", 10.0, 20.0, 30.0)


def test_a_session_without_first_turn_at_has_none(tmp_path, monkeypatch):
    """State written before this field existed: judged from its next Stop on."""
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (tmp_path / "abc.json").write_text(json.dumps({"resource": {}, "started_at": 10.0, "turn_count": 4}))
    (got,) = coverage.read_states()
    assert got.first_turn_at is None and got.harness == "claude-code"


# --- reading Loki -----------------------------------------------------------------------------

def test_the_loki_query_is_one_grouped_count_bounded_to_the_population():
    q = coverage.observed_query(["a-1", "b.2"], range_s=7200)
    assert q == ('sum by (session_id) (count_over_time({service_name="claude-code"} '
                 '| event_name="api_request" | session_id=~"a\\\\-1|b\\\\.2" [7200s]))')


def test_observed_ids_are_parsed_from_the_result():
    body = {"status": "success", "data": {"result": [
        {"metric": {"session_id": "x"}, "value": [1, "3"]},
        {"metric": {"session_id": "y"}, "value": [1, "0"]}]}}
    assert coverage.parse_observed(body) == {"x"}


def test_a_failed_query_raises_rather_than_reading_as_none_observed():
    with pytest.raises(RuntimeError):
        coverage.parse_observed({"status": "error", "error": "too many series"})


@pytest.mark.parametrize("text,want", [
    ("30d", 30 * DAY), ("720h", 30 * DAY), ("2h0m0s", 7200), ("1w", 7 * DAY), ("90m", 5400),
])
def test_loki_durations(text, want):
    assert coverage.duration_s(text) == want


@pytest.mark.parametrize("text", ["30 days", "30dx", "x30d", "", "720"])
def test_a_malformed_duration_raises_rather_than_reading_as_part_of_itself(text):
    """`30dx` read as 30 d, or `720` as nothing, would misjudge retention silently."""
    with pytest.raises(ValueError):
        coverage.duration_s(text)


@pytest.mark.parametrize("cfg,want", [
    ({"limits_config": {"retention_period": "30d"}, "compactor": {"retention_enabled": True}}, 30 * DAY),
    ({"limits_config": {"retention_period": "0s"}, "compactor": {"retention_enabled": True}}, None),
    ({"limits_config": {"retention_period": "12h"}, "compactor": {"retention_enabled": False}}, None),
    # table_manager carries its own, unused retention_period: never the one read
    ({"limits_config": {"retention_period": "30d"}, "table_manager": {"retention_period": "1h"},
      "compactor": {"retention_enabled": True}}, 30 * DAY),
])
def test_retention_is_limits_configs_and_none_means_kept(cfg, want):
    """None: kept for ever, which is never shorter than the window."""
    assert coverage.retention_from_config(cfg) == want


def test_unlimited_retention_judges_normally():
    assert judge([s("x")], observed={"x"}, retention_s=None).outcome == "pass"


def test_the_query_looks_back_a_bounded_reach_whatever_the_sessions_age():
    """#166 round 3: a months-long session must not make every run count months of events."""
    assert coverage.LOOKBACK_S == 7 * DAY
    seen = {}

    def observed(ids, since):
        seen["since"] = since
        return set(ids)
    for keep, reach in ((30 * DAY, 7 * DAY), (None, 7 * DAY), (2 * DAY, 2 * DAY)):
        coverage.native_coverage(states=lambda: [s("a", first_turn_ago=90 * DAY, last_turn_ago=3600)],
                                 observed=observed, retention=lambda: keep, now=NOW)
        assert NOW - seen["since"] == reach


def test_a_first_turn_beyond_reach_is_judged_by_its_last_turn():
    """A long-running session's first turn is out of reach; its last completed turn
    is evidence the query can see."""
    old = dict(first_turn_ago=40 * DAY, started_ago=40 * DAY)
    assert judge([s("long", last_turn_ago=2 * 3600, **old)]).outcome == "fail"
    assert judge([s("long", last_turn_ago=120, **old)]).outcome == "pending"
    c = judge([s("long", last_turn_ago=9 * DAY, **old)])
    assert c.outcome == "unknown" and "long" in c.detail


def test_a_busy_session_is_judged_by_its_first_turn_not_its_latest():
    """A session taking a turn every few minutes always has a latest turn inside
    the grace period. Its first turn is what makes it judgeable."""
    assert judge([s("busy", first_turn_ago=2 * 3600, last_turn_ago=120)]).outcome == "fail"


def test_state_written_before_first_turn_at_is_judged_by_its_last_turn():
    """Sessions running when this ships have no first_turn_at, but their state
    already holds the last completed turn: they are judged now, not after upgrade."""
    assert judge([s("legacy", first_turn_ago=None, last_turn_ago=2 * 3600)]).outcome == "fail"
    assert judge([s("legacy", first_turn_ago=None, last_turn_ago=2 * 3600)], observed={"legacy"}).outcome == "pass"


def test_last_turn_is_read_from_state_and_zero_means_none(tmp_path, monkeypatch):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (tmp_path / "a.json").write_text(json.dumps({"started_at": 1.0, "open_prompt_started_at": 5.0}))
    (tmp_path / "b.json").write_text(json.dumps({"started_at": 1.0, "open_prompt_started_at": 0.0}))
    got = {x.session_id: x.last_turn_at for x in coverage.read_states()}
    assert got == {"a": 5.0, "b": None}


def test_a_first_turn_at_zero_is_a_turn_not_an_absence():
    x = s("z")
    x = Session(x.session_id, x.harness, x.repo, x.started_at, 0.0, x.written_at)
    assert coverage.population([x], NOW) == [x]


def test_no_query_when_nobody_is_judged():
    def observed(ids, since):
        raise AssertionError("queried Loki with nothing to judge")
    c = coverage.native_coverage(states=lambda: [], observed=observed, retention=lambda: None, now=NOW)
    assert c.outcome == "pending"


# --- wiring -----------------------------------------------------------------------------------

def test_it_is_one_of_the_doctors_checks():
    assert doctor.native_coverage in doctor.CHECKS
    assert doctor.native_coverage.check_name == "native coverage"


# --- first_turn_at is persisted at Stop ------------------------------------------------------

def _assistant(ts):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "model": "claude-opus-5", "usage": {"input_tokens": 3, "output_tokens": 2}, "content": []}})


def _user(ts, prompt):
    return json.dumps({"type": "user", "timestamp": ts, "promptId": prompt,
                       "message": {"role": "user", "content": "hi"}})


def test_first_turn_at_is_set_once_at_stop_and_survives_a_reload(tmp_path, monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from stdtel.hooks import cli as hooks
    from stdtel.state import SessionState
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([_user("2026-10-09T09:00:00.000Z", "p1"), _assistant("2026-10-09T09:00:01.000Z")]) + "\n")
    hooks.session_start({"session_id": "ft-1", "cwd": str(tmp_path)})
    assert SessionState.load("ft-1").first_turn_at is None
    hooks.stop({"session_id": "ft-1", "cwd": str(tmp_path), "transcript_path": str(t)}, exporter=InMemorySpanExporter())
    first = SessionState.load("ft-1").first_turn_at
    assert first == pytest.approx(1791536400.0)
    with t.open("a") as f:
        f.write(_user("2026-10-09T10:00:00.000Z", "p2") + "\n" + _assistant("2026-10-09T10:00:01.000Z") + "\n")
    hooks.stop({"session_id": "ft-1", "cwd": str(tmp_path), "transcript_path": str(t)}, exporter=InMemorySpanExporter())
    assert SessionState.load("ft-1").first_turn_at == first, "set once, never moved"


def test_a_session_already_past_its_first_turn_gets_it_at_its_next_stop(tmp_path, monkeypatch):
    """The sessions this check exists for are long-running and already had turns
    when the field shipped. They are judged from their next Stop."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from stdtel.hooks import cli as hooks
    from stdtel.state import SessionState
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    hooks.session_start({"session_id": "ft-2", "cwd": str(tmp_path)})
    with SessionState.mutate("ft-2") as st:
        st.turn_count = 40
    t = tmp_path / "t.jsonl"
    t.write_text(_user("2026-10-09T11:00:00.000Z", "p9") + "\n" + _assistant("2026-10-09T11:00:01.000Z") + "\n")
    hooks.stop({"session_id": "ft-2", "cwd": str(tmp_path), "transcript_path": str(t)}, exporter=InMemorySpanExporter())
    assert SessionState.load("ft-2").first_turn_at == pytest.approx(1791543600.0)


# --- council round 1 on #166: one bad file never sinks the check ----------------------------

@pytest.mark.parametrize("raw", [
    {"resource": [], "started_at": 1.0, "first_turn_at": 2.0},                 # resource not an object
    {"resource": {}, "started_at": "soon", "first_turn_at": 2.0},              # started_at not a number
    {"resource": {}, "started_at": 1.0, "first_turn_at": "yesterday"},         # first_turn_at not a number
    {"resource": {}, "started_at": 1.0, "first_turn_at": float("nan")},
    {"resource": {}, "started_at": float("inf"), "first_turn_at": 2.0},
    {"resource": {}, "started_at": 1e300, "first_turn_at": 2.0},               # localtime() would raise
    {"resource": {"std.repo": 7}, "started_at": 1.0, "first_turn_at": 2.0},
    [1, 2],                                                                     # not an object at all
])
def test_a_state_file_with_a_malformed_field_is_skipped_alone(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (tmp_path / "bad.json").write_text(json.dumps(raw))
    (tmp_path / "good.json").write_text(json.dumps({"resource": {}, "started_at": 1.0, "first_turn_at": 2.0}))
    assert [x.session_id for x in coverage.read_states()] == ["good"]


def test_a_session_id_that_is_not_id_shaped_is_never_put_in_a_query(tmp_path, monkeypatch):
    """Ids reach LogQL inside a quoted regex. A file named with a quote or a
    backslash is not a Claude Code session, and must not shape the query."""
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    for name in ('we"ird', "back\\slash", "ok-1_a.b"):
        (tmp_path / f"{name}.json").write_text(json.dumps({"resource": {}, "started_at": 1.0, "first_turn_at": 2.0}))
    assert [x.session_id for x in coverage.read_states()] == ["ok-1_a.b"]
    with pytest.raises(ValueError):
        coverage.observed_query(['x"y'], 60)


def test_ids_are_queried_in_bounded_batches(monkeypatch):
    calls = []

    def fetch(query):
        calls.append(query)
        return {"status": "success", "data": {"result": []}}
    ids = [f"s{i:04d}" for i in range(coverage.BATCH * 2 + 1)]
    assert coverage.read_observed(ids, since=0, fetch=fetch, now=10_000) == set()
    assert coverage.BATCH <= 100, "a batch must stay well inside a URL"
    assert len(calls) == 3 and all(q.count("s0") <= coverage.BATCH for q in calls)


def test_credentials_in_the_loki_url_never_reach_the_output(monkeypatch):
    monkeypatch.setenv("STDTEL_LOKI", "http://user:s3cret@loki.example:3100")

    def down(ids, since):
        raise ConnectionRefusedError()
    c = coverage.native_coverage(states=lambda: [s("x")], observed=down, retention=lambda: None, now=NOW)
    assert c.outcome == "unknown" and "s3cret" not in c.detail and "loki.example:3100" in c.detail


def test_stream_retention_rules_are_not_evaluated():
    """#166 round 3: taking the minimum of every stream rule let an unrelated
    short-lived stream make the check unknown, and evaluating selectors and
    priority is Loki's job. Only the global period is read; the docs say so."""
    cfg = {"compactor": {"retention_enabled": True},
           "limits_config": {"retention_period": "30d",
                             "retention_stream": [{"selector": '{app="other"}', "period": "12h"}]}}
    assert coverage.retention_from_config(cfg) == 30 * DAY


@pytest.mark.parametrize("text", ["1h1h", "1m1h", "1s1ms1s"])
def test_units_must_appear_once_largest_first(text):
    with pytest.raises(ValueError):
        coverage.duration_s(text)


# --- council round 2 on #166: nothing in the check raises -------------------------------------

@pytest.mark.parametrize("url", ["http://localhost:notaport", "http://[::1", "http://[::1]:3100",
                                 "not a url", "http://u:p@[::1]:3100"])
def test_a_malformed_loki_url_is_reported_not_raised(monkeypatch, url):
    monkeypatch.setenv("STDTEL_LOKI", url)

    def down(ids, since):
        raise ConnectionRefusedError()
    c = coverage.native_coverage(states=lambda: [s("x")], observed=down, retention=lambda: None, now=NOW)
    assert c.outcome == "unknown" and ":p@" not in c.detail
    assert c.detail.startswith("cannot read Loki"), "the Loki failure is named, not lost to the outer guard"


def test_an_ipv6_loki_url_is_shown_bracketed(monkeypatch):
    monkeypatch.setenv("STDTEL_LOKI", "http://user:pw@[::1]:3100")
    assert coverage._loki_shown() == "http://[::1]:3100"


def test_a_start_time_localtime_rejects_still_names_the_session(monkeypatch):
    def boom(_):
        raise OverflowError("timestamp out of range for platform time_t")
    monkeypatch.setattr(coverage.time, "localtime", boom)
    c = judge([s("dead")])
    assert c.outcome == "fail" and "dead" in c.detail


def test_anything_the_judge_raises_is_unknown(monkeypatch):
    monkeypatch.setattr(coverage, "judge", lambda *a, **kw: 1 / 0)
    c = coverage.native_coverage(states=lambda: [s("x")], observed=lambda i, t: {"x"}, retention=lambda: None, now=NOW)
    assert c.outcome == "unknown" and "ZeroDivisionError" in c.detail and c.remedy


@pytest.mark.parametrize("raw", [
    {"resource": False, "started_at": 1.0, "first_turn_at": 2.0},
    {"resource": {"std.harness": False}, "started_at": 1.0, "first_turn_at": 2.0},
    {"resource": {"std.repo": False}, "started_at": 1.0, "first_turn_at": 2.0},
    {"resource": {}, "started_at": False, "first_turn_at": 2.0},
    {"resource": {}, "started_at": 1.0, "first_turn_at": False},
])
def test_a_false_value_is_malformed_not_a_default(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path))
    (tmp_path / "bad.json").write_text(json.dumps(raw))
    assert coverage.read_states() == []


def test_nobody_to_judge_reads_no_loki_at_all():
    def boom():
        raise AssertionError("read Loki's config with nothing to judge")
    c = coverage.native_coverage(states=lambda: [], observed=lambda i, t: set(), retention=boom, now=NOW)
    assert c.outcome == "pending"
