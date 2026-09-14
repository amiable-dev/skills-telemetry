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


def test_metrics_pipeline_drops_service_instance_id():
    """prometheusremotewrite maps it to `instance`, and a per-process value there
    gives every span its own series — a counter stuck at 1 and a rate of 0 (#42).

    stdtel pins the value now, but Copilot's native SDK does not and we do not
    control it, so the metrics pipeline drops it regardless of who sent it.
    """
    import yaml
    cfg = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())
    processor = next(k for k in cfg["processors"] if k.startswith("resource/"))
    deleted = {a["key"] for a in cfg["processors"][processor]["attributes"]
               if a["action"] == "delete"}
    assert "service.instance.id" in deleted
    assert processor in cfg["service"]["pipelines"]["metrics"]["processors"], \
        "declared but not in the pipeline drops nothing"
    assert processor not in cfg["service"]["pipelines"]["traces"]["processors"], \
        "traces keep it: per-machine detail is wanted there, and costs no series"


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


# --- ADR-006: the Langfuse overlay ---

def test_langfuse_overlay_duplicates_rather_than_moves_attributes():
    """std.* must survive: Tempo, the warehouse loader and spanmetrics all read it."""
    overlay = (ROOT / "collector" / "overlay-langfuse.yaml").read_text()
    assert "delete_key" not in overlay and "delete_matching_keys" not in overlay
    for attr in ("std.skill.name", "std.skill.version", "std.ticket.id", "std.team", "std.harness"):
        assert f'attributes["{attr}"]' in overlay, f"{attr} not carried into Langfuse"


def test_langfuse_overlay_uses_the_filterable_prefix():
    """Anything not prefixed langfuse.trace.metadata.* is unqueryable in Langfuse."""
    import re
    overlay = (ROOT / "collector" / "overlay-langfuse.yaml").read_text()
    targets = re.findall(r'set\(attributes\["(langfuse[^"]+)"\]', overlay)
    assert targets, "overlay sets no langfuse attributes"
    for t in targets:
        assert t.startswith("langfuse.trace.metadata.") or t == "langfuse.session.id", t


def test_default_overlay_is_a_no_op():
    """The base stack must not acquire an exporter it cannot reach.

    Checks the parsed structure, not the prose — the file's comments explain what
    the overlay mechanism is for and legitimately mention Langfuse.
    """
    import yaml
    cfg = yaml.safe_load((ROOT / "collector" / "overlay-none.yaml").read_text()) or {}
    assert "exporters" not in cfg and "processors" not in cfg and "receivers" not in cfg
    assert "pipelines" not in (cfg.get("service") or {})


def test_langfuse_credentials_are_not_committed():
    import subprocess
    tracked = subprocess.run(["git", "ls-files", "deploy/.env"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
    assert tracked == "", "deploy/.env must stay gitignored"
    overlay = (ROOT / "collector" / "overlay-langfuse.yaml").read_text()
    assert "${env:LANGFUSE_AUTH}" in overlay, "auth must come from the environment"


def test_down_stops_the_optional_profile_too():
    """`docker compose down` ignores profiled services unless named.

    Without --profile langfuse, `make down` removed the base stack, left six
    Langfuse containers running, and could not delete the network — reported as
    "Resource is still in use", which reads as a Docker problem rather than a
    missing flag.
    """
    makefile = (ROOT / "Makefile").read_text()
    down = next(l for l in makefile.splitlines() if l.startswith("down:"))
    assert "--profile langfuse" in down, "down must cover the opt-in services"


def test_user_hash_is_read_from_the_resource():
    """It is a resource attribute, set once at SessionStart, not per span (#43)."""
    mod = loader()
    row = mod.parse_span({"std.skill.name": "s"}, {"std.user.hash": "3f9a1c7e0b2d4a86"},
                         {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"})
    assert row["user_hash"] == "3f9a1c7e0b2d4a86"


def test_content_hash_reaches_the_warehouse():
    mod = loader()
    row = mod.parse_span({"std.skill.name": "s", "std.skill.content_hash": "a1b2c3d4e5f60718"}, {},
                         {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"})
    assert row["content_hash"] == "a1b2c3d4e5f60718"


def test_content_hash_is_not_a_spanmetrics_dimension():
    """A new Prometheus series per skill edit is #42 again.

    The hash belongs in traces and the warehouse, where cardinality costs
    nothing, and nowhere near a metric label.
    """
    import yaml
    cfg = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())
    dimensions = {d["name"] for d in cfg["connectors"]["spanmetrics"]["dimensions"]}
    assert "std.skill.content_hash" not in dimensions


def test_every_loader_column_exists_in_the_schema():
    """Including the ones added by ALTER after the table was first created.

    `CREATE TABLE IF NOT EXISTS` does nothing to an existing database, so a
    column added only to the CREATE reaches new warehouses and no others — and
    the loader discovers that on its first INSERT after an upgrade.
    """
    schema = (ROOT / "warehouse" / "schema.sql").read_text()
    create = schema.split("CREATE TABLE IF NOT EXISTS skill_invocation (")[1].split(");")[0]
    declared = {line.strip().split()[0] for line in create.splitlines() if line.strip()
                and not line.strip().startswith("--")}
    added = set(re.findall(r"ALTER TABLE skill_invocation ADD COLUMN IF NOT EXISTS (\w+)", schema))
    for column in loader().COLS:
        assert column in declared, f"{column} is not in the CREATE TABLE"
    # anything added after the first release must also be reachable by migration
    assert "content_hash" in added, "a column added later needs an ALTER for existing warehouses"
