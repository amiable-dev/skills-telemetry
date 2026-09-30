"""ADR-014 decision 10: with the detailed view off, Claude Code names a skill from a
third-party plugin `"third-party"`. stdtel saw the skill run, under its real
name, on the same prompt. When exactly one skill ran there that the harness did
not name, the request is named from it and labelled `derived`. When more than one
could be it, it stays `"third-party"` — never guessed (#117).

Read-time, in a view, for the reason activation_change_request is one: traces
and requests are loaded by different loaders, and arrival order must not matter.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
T0 = dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc)


def _connect():
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed")
    try:
        return psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:                           # noqa: BLE001
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")


def _clean(cur):
    cur.execute("DELETE FROM llm_request WHERE request_id LIKE 'wtest-tp-%%'")
    cur.execute("DELETE FROM artefact_activation WHERE span_id LIKE 'wtest-tp-%%'")


#: prompt -> (skills stdtel saw run, [(request id, skill name the harness sent)])
CASES = {
    "one":       (["acme-lint"],                  [("r1", "third-party"), ("r2", None)]),
    "two":       (["acme-lint", "acme-format"],   [("r3", "third-party")]),
    "named":     (["acme-lint", "probe-echo"],    [("r4", "third-party"), ("r5", "probe-echo")]),
    "none-seen": ([],                             [("r6", "third-party")]),
    "sub-too":   (["acme-lint"],                  [("r7", "third-party")]),
}


@pytest.fixture(scope="module")
def rows():
    conn = _connect()
    with conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        _clean(cur)
        for prompt, (skills, requests) in CASES.items():
            pid = f"wtest-tp-prompt-{prompt}"
            for i, name in enumerate(skills):
                cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, "
                            "ended_at, kind, name, prompt_id, source) VALUES (%s,'t','wtest-tp-sess',%s,%s,"
                            "'skill',%s,%s,'hook')", (f"wtest-tp-{prompt}-{i}", T0, T0, name, pid))
            if prompt == "sub-too":        # a sub-agent and a turn on the same prompt are not skills
                for kind in ("subagent", "turn"):
                    cur.execute("INSERT INTO artefact_activation (span_id, trace_id, session_id, started_at, "
                                "ended_at, kind, name, prompt_id, source) VALUES (%s,'t','wtest-tp-sess',%s,%s,"
                                "%s,'Explore',%s,'hook')", (f"wtest-tp-{prompt}-{kind}", T0, T0, kind, pid))
            for rid, skill in requests:
                cur.execute("INSERT INTO llm_request (harness, request_id, session_id, prompt_id, ended_at, "
                            "skill_name, cost_usd, attribution_source) VALUES ('claude-code', %s, "
                            "'wtest-tp-sess', %s, %s, %s, 0.01, 'native')", (f"wtest-tp-{rid}", pid, T0, skill))
        cur.execute("SELECT request_id, skill_name, attribution_source FROM llm_request_attributed "
                    "WHERE request_id LIKE 'wtest-tp-%%'")
        got = {r[0].removeprefix("wtest-tp-"): (r[1], r[2]) for r in cur.fetchall()}
        _clean(cur)
    conn.close()
    return got


def test_one_unnamed_skill_names_the_request(rows):
    assert rows["r1"] == ("acme-lint", "derived")


def test_a_request_outside_any_skill_is_untouched(rows):
    assert rows["r2"] == (None, "native")


def test_two_candidates_are_never_guessed_between(rows):
    assert rows["r3"] == ("third-party", "native")


def test_a_skill_the_harness_named_itself_is_not_a_candidate(rows):
    """probe-echo is a project skill: Claude Code names it, so the only skill it
    could not name is acme-lint."""
    assert rows["r4"] == ("acme-lint", "derived")
    assert rows["r5"] == ("probe-echo", "native")


def test_with_nothing_seen_it_stays_third_party(rows):
    assert rows["r6"] == ("third-party", "native")


def test_only_skill_activations_are_candidates(rows):
    assert rows["r7"] == ("acme-lint", "derived")


def test_every_request_appears_once(rows):
    assert len(rows) == sum(len(r) for _, r in CASES.values())


def test_the_view_has_every_column_of_the_table():
    """So a panel can switch from llm_request to the view without losing one."""
    from tests.test_warehouse import schema_columns
    view = re.search(r"CREATE OR REPLACE VIEW llm_request_attributed AS(.*?);\n", SCHEMA, re.S).group(1)
    for col in schema_columns("llm_request"):
        assert re.search(rf"\b{col}\b", view), col
