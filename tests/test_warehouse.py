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


# --- ADR-009: the span is an artefact activation, and skills are one kind ---

def activation_span(attrs: dict, span: dict | None = None) -> dict:
    base = {"spanID": "s", "traceID": "t", "startTimeUnixNano": "0"}
    base.update(span or {})
    return loader().parse_activation(attrs, {}, base)


def test_every_activation_loader_column_exists_in_the_schema():
    missing = set(loader().ACTIVATION_COLS) - set(schema_columns("artefact_activation"))
    assert not missing, f"loader writes columns the schema lacks: {sorted(missing)}"


def test_parse_activation_produces_exactly_the_activation_columns():
    mod = loader()
    assert set(activation_span({})) == set(mod.ACTIVATION_COLS)


def test_loader_run_columns_exist_in_the_schema():
    missing = set(loader().LOADER_RUN_COLS) - set(schema_columns("loader_run"))
    assert not missing, f"loader writes columns the schema lacks: {sorted(missing)}"


def test_session_columns_added_after_the_first_release_have_a_migration():
    """CREATE TABLE IF NOT EXISTS does nothing to an existing warehouse (#55).

    cost_usd, api_ms, tool_ms and duration_ms are now read from the harness's own
    cost-state entry. Declaring them only in the CREATE would give them to new
    warehouses and to nobody else, and the loader would find out on its first
    INSERT after the upgrade.
    """
    added = set(re.findall(r"ALTER TABLE session_cost ADD COLUMN IF NOT EXISTS (\w+)", SCHEMA))
    for column in ("api_ms", "tool_ms", "duration_ms"):
        assert column in added, f"{column} unreachable in an existing warehouse"
        assert column in schema_columns("session_cost"), f"{column} missing from the CREATE"


def test_a_legacy_skill_invocation_span_is_read_as_kind_skill():
    """Rows recorded before ADR-009 carry no std.artefact.kind and must keep loading."""
    row = activation_span({"std.skill.name": "structured-logging",
                           "std.skill.llm_requests": 4})
    assert row["kind"] == "skill"
    assert row["name"] == "structured-logging"
    assert row["llm_requests"] == 4


def test_a_turn_activation_carries_no_name():
    """prompt_id is unbounded and `name` is a spanmetrics dimension (ADR-009)."""
    row = activation_span({"std.artefact.kind": "turn", "std.prompt.id": "p-1",
                           "std.turn.llm_requests": 3})
    assert row["name"] is None
    assert row["prompt_id"] == "p-1" and row["llm_requests"] == 3


def test_an_unobserved_token_count_is_null_never_zero():
    """ADR-005. A sub-agent that read no cached tokens and one whose cache reads
    were never observed are different facts, and sum() over a column that
    conflates them is not a measurement."""
    row = activation_span({"std.artefact.kind": "subagent", "std.subagent.type": "Explore"})
    for column in ("input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_creation_tokens", "duration_ms", "tool_calls"):
        assert row[column] is None, f"{column} invented a zero"


def test_each_kind_reads_its_own_request_count():
    """The schema is discriminated: a sub-agent's requests and a turn's are
    different measurements that happen to share a column."""
    mod = loader()
    assert activation_span({"std.artefact.kind": "subagent", "std.subagent.llm_requests": 9,
                            "std.subagent.tool_calls": 12})["llm_requests"] == 9
    assert activation_span({"std.artefact.kind": "turn", "std.prompt.id": "p",
                            "std.turn.llm_requests": 2})["llm_requests"] == 2
    # a compaction has no request count at all, and must not borrow one
    assert activation_span({"std.artefact.kind": "compaction",
                            "std.compaction.reason": "auto"})["llm_requests"] is None


def test_hook_latency_keeps_basenames_and_drops_the_path():
    row = activation_span({"std.artefact.kind": "turn", "std.prompt.id": "p",
                           "std.turn.hook_ms": 312,
                           "std.turn.hook.stdtel-hook.ms": 12,
                           "std.turn.hook.format-sh.ms": 300})
    import json as _json
    assert row["hook_ms"] == 312
    assert _json.loads(row["hook_ms_by_hook"]) == {"stdtel-hook": 12, "format-sh": 300}


def test_a_turn_that_reported_no_hooks_has_no_breakdown():
    """An empty object would claim the turn ran no hooks; the Stop hook always
    runs, so the truthful value is 'not reported'."""
    row = activation_span({"std.artefact.kind": "turn", "std.prompt.id": "p"})
    assert row["hook_ms_by_hook"] is None


def test_compaction_estimates_are_recorded_as_received():
    row = activation_span({"std.artefact.kind": "compaction",
                           "std.compaction.reason": "auto",
                           "std.compaction.tokens_before": "954000",
                           "std.compaction.tokens_after": "31000",
                           "std.compaction.turns_since_previous": "44"})
    assert row["compaction_reason"] == "auto"
    assert (row["compaction_tokens_before"], row["compaction_tokens_after"]) == (954000, 31000)
    assert row["compaction_turns_since_previous"] == 44


def test_subagent_parent_prompt_id_reaches_the_warehouse():
    row = activation_span({"std.artefact.kind": "subagent", "std.subagent.type": "Explore",
                           "std.subagent.id": "a-7", "std.subagent.depth": 1,
                           "std.artefact.parent_prompt_id": "p-3",
                           "std.artefact.name": "Explore"})
    assert row["subagent_type"] == "Explore" and row["subagent_id"] == "a-7"
    assert row["subagent_depth"] == 1 and row["parent_prompt_id"] == "p-3"
    assert row["name"] == "Explore"


def test_source_says_whether_a_value_was_observed_or_inferred():
    """A sub-agent read out of its transcript because no hook fired is not the
    same measurement as one the harness handed us (ADR-005)."""
    assert activation_span({"std.artefact.kind": "subagent", "std.artefact.source": "transcript",
                            "std.subagent.type": "Explore"})["source"] == "transcript"
    assert activation_span({"std.artefact.kind": "subagent",
                            "std.subagent.type": "Explore"})["source"] is None


def test_session_cost_carries_real_money_and_harness_wall_time():
    """cost_usd was NULL in every row since the schema was written, because
    nothing read the harness's cost-state entry."""
    mod = loader()
    attrs = {"session.id": "s1", "std.session.cost_usd": 366.42,
             "std.session.api_ms": 1234, "std.session.tool_ms": 567,
             "std.session.duration_ms": 9999}
    row = mod.parse_session(attrs, {}, {"startTimeUnixNano": "0", "endTimeUnixNano": "0"})
    assert row["cost_usd"] == 366.42
    assert (row["api_ms"], row["tool_ms"], row["duration_ms"]) == (1234, 567, 9999)


def test_a_session_with_no_reported_cost_is_null_not_zero():
    mod = loader()
    row = mod.parse_session({"session.id": "s1"}, {},
                            {"startTimeUnixNano": "0", "endTimeUnixNano": "0"})
    assert row["cost_usd"] is None
    assert row["api_ms"] is None and row["tool_ms"] is None and row["duration_ms"] is None


def test_the_loader_searches_both_span_names():
    """The rename is wire-level; rows emitted before it are still in Tempo."""
    source = (ROOT / "warehouse" / "load_traces.py").read_text()
    assert 'std.artefact.activation' in source
    assert 'std.skill.invocation' in source


def test_the_span_names_match_the_capture_side():
    """One constant per name, agreed between the emitter and the loader."""
    from stdtel import artefact
    mod = loader()
    assert mod.SPAN_NAME == artefact.SPAN_NAME
    assert mod.LEGACY_SKILL_SPAN_NAME == artefact.LEGACY_SKILL_SPAN_NAME
    assert mod.SESSION_SPAN_NAME == artefact.SESSION_SPAN_NAME


def test_every_kind_the_capture_side_emits_is_a_kind_the_loader_can_read():
    from stdtel import artefact
    for kind in artefact.KINDS:
        row = activation_span({"std.artefact.kind": kind, "std.prompt.id": "p"})
        assert row["kind"] == kind


def test_no_attribute_outside_the_capture_allowlist_reaches_a_column():
    """The per-kind allowlist is the privacy control (ADR-009). A column fed by
    an attribute that no kind may carry would route around it."""
    from stdtel import artefact
    mod = loader()
    import re as _re
    source = (ROOT / "warehouse" / "load_traces.py").read_text()
    # every std.* / gen_ai.* key the activation parser reads
    body = source.split("def parse_activation(")[1].split("\ndef ")[0]
    read = set(_re.findall(r'g\("((?:std|gen_ai|session)\.[^"]+)"', body))
    # `g` falls back to the resource, which carries the join keys set once at
    # SessionStart rather than per span, so those are permitted too.
    from stdtel.enrich import resource_attributes
    permitted = set().union(*artefact.ALLOWED.values()) | set(resource_attributes())
    unknown = read - permitted
    assert not unknown, f"loader reads attributes no kind may carry: {sorted(unknown)}"


def test_a_search_api_span_id_is_not_mistaken_for_base64():
    """A 16-character hex span id is also valid base64.

    b64decode does not raise on it: it returns twelve bytes of a different id,
    which the loader would have written as the primary key. The length is the
    only reliable discriminator — an id is 8 bytes or 16, never 12.
    """
    mod = loader()
    row = mod.parse_span({}, {}, {"spanID": "40ce813450c64c51",
                                  "traceID": "bec8f538b1859ce9aee65c310b1ca7f6",
                                  "startTimeUnixNano": "0"})
    assert row["span_id"] == "40ce813450c64c51"
    assert row["trace_id"] == "bec8f538b1859ce9aee65c310b1ca7f6"


# --- #67: the loader window must outlast Tempo's flush delay ------------------

def test_the_default_loader_window_outlasts_tempos_flush_delay():
    """A span is not searchable the moment it arrives.

    Every `subagent` activation carries the sub-agent's own start and end and is
    not sent until the parent session's next Stop, so its timestamp is always in
    the past. Measured: one stamped 90 minutes back was invisible to search
    immediately and present about half an hour later, with nothing discarded and
    no error anywhere. A loader window narrower than that delay steps over those
    spans and never comes back for them, because each run only looks forward.
    """
    from warehouse.load_traces import MIN_SAFE_WINDOW_HOURS, TEMPO_FLUSH_MINUTES

    assert MIN_SAFE_WINDOW_HOURS * 60 > TEMPO_FLUSH_MINUTES * 2, \
        "the safe window must leave real headroom over the flush delay, not just clear it"


def test_a_narrow_window_says_so_rather_than_quietly_missing_rows(capsys, monkeypatch):
    """ADR-005: a component that cannot do its job says so."""
    import warehouse.load_traces as lt

    monkeypatch.setattr(lt, "record_run", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("no tempo in this test")

    monkeypatch.setitem(__import__("sys").modules, "requests",
                        type("m", (), {"get": staticmethod(boom)})())
    try:
        lt.main(["--tempo", "http://127.0.0.1:1", "--dsn", "postgresql://x", "--since", "1h"])
    except Exception:
        pass
    err = capsys.readouterr().err
    assert "narrower than Tempo's flush delay" in err
    assert "span_id" in err, "say why overlapping is free, or the advice reads as a cost"


def test_the_shipped_scheduled_loader_uses_a_safe_window():
    """The compose loader is the deployed form; a narrow window there would lose
    exactly the sub-agent rows the efficiency queries exist to show."""
    import re

    import yaml

    from warehouse.load_traces import MIN_SAFE_WINDOW_HOURS

    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())
    command = " ".join(str(x) for x in compose["services"]["loader"]["command"])
    traces = [m for m in re.findall(r"load_traces[^\n|]*", command)]
    assert traces, "the loader service must run load_traces"
    for invocation in traces:
        since = re.search(r"--since\s+(\d+)h", invocation)
        hours = int(since.group(1)) if since else 24      # the argparse default
        assert hours >= MIN_SAFE_WINDOW_HOURS, f"{invocation!r} uses a {hours}h window"
