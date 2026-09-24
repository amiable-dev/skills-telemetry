"""Provisioned Grafana dashboards.

The dashboard named "Skill scorecard" showed none of the scorecard — four
Prometheus panels of operational metrics, and no Postgres datasource at all. The
first-time pass rate, the with/without arms and the recommendation lived only in
`warehouse/scorecard.sql`, which nothing surfaced.

These tests check the provisioning is coherent and that panel SQL refers to
objects that exist. They cannot check that a panel *reads well*; that needs eyes.

ADR-009 added a second concern: the dashboards must query span names and metric
labels that something actually emits. Renaming the span broke every Prometheus
panel and nothing failed, because a panel pointed at a name nothing emits
renders an empty graph — indistinguishable from a quiet week.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from stdtel import artefact

ROOT = Path(__file__).resolve().parent.parent
PROVISIONING = ROOT / "deploy" / "grafana" / "provisioning"
DASHBOARDS = sorted((PROVISIONING / "dashboards").glob("*.json"))
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()


def datasources() -> list[dict]:
    return yaml.safe_load((PROVISIONING / "datasources" / "ds.yaml").read_text())["datasources"]


def panels(dashboard: Path) -> list[dict]:
    return json.loads(dashboard.read_text()).get("panels", [])


def test_dashboards_are_valid_json():
    assert DASHBOARDS
    for d in DASHBOARDS:
        json.loads(d.read_text())


def test_a_postgres_datasource_is_provisioned():
    """The outcome metrics live in Postgres; without this they cannot be shown."""
    types = {d["type"] for d in datasources()}
    assert "grafana-postgresql-datasource" in types or "postgres" in types, types


def test_every_datasource_pins_its_uid():
    """An unpinned datasource gets a random uid, which no committed dashboard can
    reference. The panels render empty and nothing reports an error."""
    for d in datasources():
        assert d.get("uid"), f"{d['name']} has no uid"


def test_every_datasource_a_panel_uses_is_provisioned():
    """Panels reference datasources by uid, so that is what must match.

    Comparing against names passed while Grafana was generating a random uid for
    the unpinned datasource, leaving the panels pointing at nothing.
    """
    provisioned = {d.get("uid") or d["name"] for d in datasources()}
    for dash in DASHBOARDS:
        for panel in panels(dash):
            for target in panel.get("targets", []):
                ds = target.get("datasource") or panel.get("datasource")
                if isinstance(ds, dict) and ds.get("uid"):
                    assert ds["uid"] in provisioned or ds["uid"].startswith("${"), \
                        f"{dash.name}/{panel.get('title')}: unknown datasource {ds}"


def test_panel_sql_references_real_tables():
    """Catches a dashboard written against a schema that has since changed."""
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
    for dash in DASHBOARDS:
        for panel in panels(dash):
            for target in panel.get("targets", []):
                sql = target.get("rawSql")
                if not sql:
                    continue
                # a query's own CTEs are valid targets; derive them rather than
                # hardcoding, or the list goes stale the next time the SQL changes.
                # Comments are stripped first: a CTE introduced by an explanatory
                # line is still a CTE, and the pattern cannot span one.
                bare = re.sub(r"--[^\n]*", "", sql)
                ctes = set(re.findall(r"(?:WITH|,)\s*(\w+)\s+AS\s*\(", bare, re.I))
                for ref in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", sql):
                    assert ref in tables or ref in ctes, \
                        f"{dash.name}/{panel.get('title')}: unknown table {ref!r}"


def test_the_scorecard_dashboard_shows_the_scorecard():
    """A dashboard called "scorecard" that shows none of it is worse than none:
    it looks like the metric is covered."""
    # check the whole definition, not only titles: the recommendation is a column
    blob = " ".join(d.read_text().lower() for d in DASHBOARDS)
    assert "pass rate" in blob or "pass_rate" in blob, "the primary metric must be shown"
    assert "recommended_action" in blob, "keep/refine/deprecate should be visible"
    assert "insufficient-data" in blob, "the refusal must reach the reader, not just the query"


def test_data_quality_is_surfaced():
    """unattributed and unversioned bound every conclusion drawn from this data,
    and are only fixable while the work is happening."""
    blob = " ".join(json.dumps(json.loads(d.read_text())) for d in DASHBOARDS)
    assert "unattributed" in blob and "unversioned" in blob


def test_panels_state_when_a_number_is_not_trustworthy():
    """The sample-size floor must reach the person reading the chart."""
    blob = " ".join(json.dumps(json.loads(d.read_text())) for d in DASHBOARDS)
    assert "insufficient" in blob.lower() or "30" in blob


def test_dashboard_uids_are_unique():
    """Two dashboards sharing a uid means one silently does not provision.

    Introduced and caught here: renaming the operational board left it holding
    `stdtel-scorecard`, the uid the new outcomes board also claimed, so Grafana
    showed only one of them and nothing reported an error.
    """
    uids = [json.loads(d.read_text()).get("uid") for d in DASHBOARDS]
    assert all(uids), "every dashboard needs a uid, or provisioning generates one per restart"
    assert len(set(uids)) == len(uids), f"duplicate uid: {uids}"


def test_dashboard_titles_are_unique():
    titles = [json.loads(d.read_text()).get("title") for d in DASHBOARDS]
    assert len(set(titles)) == len(titles), f"duplicate title: {titles}"


# --- ADR-009: a panel must query something that exists ------------------------

COLLECTOR = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())


def _exprs():
    """(file, panel title, PromQL) for every Prometheus target."""
    for path in DASHBOARDS:
        dash = json.loads(path.read_text())
        for panel in dash.get("panels", []):
            for target in panel.get("targets", []):
                if "expr" in target:
                    yield path.name, panel["title"], target["expr"]


def _dimension_labels() -> set[str]:
    """Spanmetrics turns each dimension into a label with dots as underscores."""
    dims = COLLECTOR["connectors"]["spanmetrics"]["dimensions"]
    return {d["name"].replace(".", "_") for d in dims}


def test_there_are_dashboards_to_check():
    """Non-vacuity: this suite has shipped assertions that matched nothing."""
    assert DASHBOARDS
    assert list(_exprs())


def test_every_span_name_queried_is_one_we_emit():
    from stdtel import artefact

    emitted = {artefact.SPAN_NAME, artefact.SESSION_SPAN_NAME}
    for file, panel, expr in _exprs():
        for name in re.findall(r'span_name="([^"]+)"', expr):
            assert name in emitted, f"{file}:{panel} queries span {name!r}, which nothing emits"


def test_every_std_label_queried_is_a_configured_dimension():
    """A label that is not a spanmetrics dimension silently groups everything
    into one series or returns nothing at all."""
    known = _dimension_labels()
    for file, panel, expr in _exprs():
        used = set(re.findall(r"\b(std_[a-z0-9_]+)\b", expr))
        unknown = used - known
        assert not unknown, f"{file}:{panel} uses {sorted(unknown)}, not in spanmetrics dimensions"


def test_prompt_id_is_never_a_metrics_dimension():
    """ADR-009. A turn carries no `std.artefact.name` precisely so that an
    unbounded id cannot become a label; putting it back would open a new time
    series per turn, which is #42 one layer up."""
    labels = _dimension_labels()
    assert "std_prompt_id" not in labels
    for file, panel, expr in _exprs():
        assert "std_prompt_id" not in expr, f"{file}:{panel} groups by an unbounded id"


@pytest.mark.parametrize("kind", artefact.KINDS)
def test_each_artefact_kind_is_visible_somewhere(kind):
    """Capturing a kind nobody can see is half a feature.

    The list used to be hardcoded here and went stale the moment `external` was
    added — the test kept passing while the new kind was invisible on every
    board. Deriving it from the enum is the only version that cannot drift."""
    blob = " ".join(expr for _f, _p, expr in _exprs())
    assert f'"{kind}"' in blob, f"no panel shows kind={kind}"


def test_scope_id_is_never_a_metrics_dimension():
    """ADR-010. `std.scope.id` is unique per container instance, so a dimension
    on it opens a new time series per loop iteration — #42 one layer up, and the
    same reasoning that keeps `std.prompt.id` out. `name` and `key` are bounded
    and are the two you actually group by."""
    labels = _dimension_labels()
    assert "std_scope_id" not in labels
    assert {"std_scope_name", "std_scope_key"} <= labels, \
        "containment is unqueryable in Prometheus without name and key"
    for file, panel, expr in _exprs():
        assert "std_scope_id" not in expr, f"{file}:{panel} groups by an unbounded id"
