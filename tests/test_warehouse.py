"""The span -> Postgres contract.

std.skill.invoked_as and std.skill.plugin were emitted on spans but existed in
neither the schema nor the loader, so plugin attribution stopped at Tempo. These
tests fail if the three definitions drift apart again.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()


TYPES = "TEXT|INT|BIGINT|NUMERIC|BOOLEAN|TIMESTAMPTZ|SERIAL|JSONB"


def schema_columns(table: str) -> list[str]:
    """Column names for a table.

    Matches `name TYPE` pairs rather than reading one column per line — several
    tables declare more than one column per line, and a line-based reader
    silently returned only the first, which made this contract test pass while
    checking almost nothing.
    """
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.S).group(1)
    body = re.sub(r"--[^\n]*", "", body)          # strip comments; they contain commas and types
    return re.findall(rf"(\w+)\s+(?:{TYPES})\b", body)


def loader():
    import importlib.util
    spec = importlib.util.spec_from_file_location("load_traces", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_loader_column_exists_in_the_schema():
    missing = set(loader().COLS) - set(schema_columns("skill_invocation"))
    assert not missing, f"loader writes columns the schema lacks: {sorted(missing)}"


def test_parse_span_produces_exactly_the_loader_columns():
    mod = loader()
    row = mod.parse_span({}, {}, {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"})
    assert set(row) == set(mod.COLS)


def test_plugin_attribution_survives_the_span_to_row_hop():
    mod = loader()
    attrs = {"std.skill.name": "structured-logging",
             "std.skill.invoked_as": "epic-loop:structured-logging",
             "std.skill.plugin": "epic-loop",
             "std.skill.version": "2.3.0"}
    row = mod.parse_span(attrs, {}, {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"})
    assert row["skill_name"] == "structured-logging"
    assert row["invoked_as"] == "epic-loop:structured-logging"
    assert row["plugin"] == "epic-loop"


def test_bare_skill_falls_back_to_the_name_and_has_no_plugin():
    mod = loader()
    row = mod.parse_span({"std.skill.name": "structured-logging"}, {},
                         {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"})
    assert row["invoked_as"] == "structured-logging" and row["plugin"] is None


def test_collector_exports_plugin_as_a_metric_dimension():
    cfg = (ROOT / "collector" / "otel-collector.yaml").read_text()
    assert "std.skill.plugin" in cfg, "plugin must be groupable in Prometheus too"


# --- Tempo returns two different span shapes; the loader reads the OTLP one ---

def test_otlp_json_ids_are_decoded_to_hex():
    """/api/traces/{id} returns base64 spanId/traceId, not the search API's hex."""
    mod = loader()
    row = mod.parse_span({}, {}, {"traceId": "vsj1OLGFnOmu5lwxCxyn9g==",
                                  "spanId": "QM6BNFDGTFE=",
                                  "startTimeUnixNano": "1789033664020823040"})
    assert row["trace_id"] == "bec8f538b1859ce9aee65c310b1ca7f6"
    assert row["span_id"] == "40ce813450c64c51"


def test_search_api_hex_ids_pass_through():
    mod = loader()
    row = mod.parse_span({}, {}, {"traceID": "abc123", "spanID": "def456",
                                  "startTimeUnixNano": "0"})
    assert (row["trace_id"], row["span_id"]) == ("abc123", "def456")


def test_end_time_prefers_the_otlp_field():
    """durationNanos does not exist on the OTLP shape; ended_at was start+0."""
    mod = loader()
    row = mod.parse_span({}, {}, {"spanId": "", "traceId": "",
                                  "startTimeUnixNano": "1000000000",
                                  "endTimeUnixNano": "3000000000"})
    assert (row["ended_at"] - row["started_at"]).total_seconds() == 2.0


def test_end_time_falls_back_to_duration():
    mod = loader()
    row = mod.parse_span({}, {}, {"spanId": "", "traceId": "",
                                  "startTimeUnixNano": "1000000000",
                                  "durationNanos": "5000000000"})
    assert (row["ended_at"] - row["started_at"]).total_seconds() == 5.0


# --- issue #1: session_cost is the total-spend denominator ---

def test_session_loader_columns_exist_in_the_schema():
    mod = loader()
    missing = set(mod.SESSION_COLS) - set(schema_columns("session_cost"))
    assert not missing, f"loader writes columns the schema lacks: {sorted(missing)}"


def test_parse_session_produces_exactly_the_session_columns():
    mod = loader()
    row = mod.parse_session({}, {}, {"startTimeUnixNano": "0", "endTimeUnixNano": "0"})
    assert set(row) == set(mod.SESSION_COLS)


def test_parse_session_carries_totals_and_join_keys():
    mod = loader()
    attrs = {"session.id": "s1", "gen_ai.usage.input_tokens": 500,
             "gen_ai.usage.cache_read_input_tokens": 100, "gen_ai.request.model": "claude-opus-5"}
    resource = {"std.ticket.id": "PLAT-42", "std.team": "payments", "std.harness": "claude-code"}
    row = mod.parse_session(attrs, resource, {"startTimeUnixNano": "1000000000",
                                              "endTimeUnixNano": "61000000000"})
    assert row["session_id"] == "s1" and row["ticket_id"] == "PLAT-42"
    assert row["input_tokens"] == 500 and row["cache_read_tokens"] == 100
    assert row["active_seconds"] == 60
    assert row["cost_usd"] is None, "harness currencies differ; price downstream"
