"""Dashboards must query things that exist.

Renaming the span broke every Prometheus panel and nothing failed: a panel
pointed at a span name that no longer existed renders an empty graph, which is
indistinguishable from a quiet week. That is the same failure shape as #42 and
#44 — a component reporting success while showing nothing — so it gets a test
rather than a careful reviewer.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted((ROOT / "deploy" / "grafana" / "provisioning" / "dashboards").glob("*.json"))
COLLECTOR = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())


def _exprs():
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


@pytest.mark.parametrize("kind", ["skill", "subagent", "compaction", "turn"])
def test_each_artefact_kind_is_visible_somewhere(kind):
    """Capturing a kind nobody can see is half a feature."""
    blob = " ".join(expr for _f, _p, expr in _exprs())
    assert f'"{kind}"' in blob, f"no panel shows kind={kind}"
