"""ADR-016 decision 4 (#160): empty states say what is empty.

- A ratio with no errors is 0, not "No skill ran": the error series does not
  exist until an error happens, so a plain division returns nothing.
- Legends name their series, not the expression.
- A spend total is not coloured as an alarm. Grafana's default thresholds turn a
  stat red at 80, so $80 of spend read as a failure.
- Every panel says what an empty result means, on both dashboards.
- No expression relies on `absent()`, which a series created at 0 defeats.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from tests.collector_harness import docker_available

ROOT = Path(__file__).resolve().parent.parent
BOARDS = sorted((ROOT / "deploy" / "grafana" / "provisioning" / "dashboards").glob("*.json"))
#: Grafana's warning and failure colours. A spend total in either reads as something wrong.
ALARM = {"red", "dark-red", "semi-dark-red", "light-red", "super-light-red", "orange", "dark-orange",
         "semi-dark-orange", "light-orange", "yellow", "dark-yellow", "semi-dark-yellow", "light-yellow",
         "#f2495c", "#e02f44", "#ff9830", "#fade2a"}


def panels():
    for f in BOARDS:
        stack = list(json.loads(f.read_text())["panels"])
        while stack:
            p = stack.pop(0)
            stack[:0] = p.get("panels", [])
            if p["type"] != "row":
                yield f.name, p


ALL = list(panels())


def _id(item):
    return f"{item[0]}/{item[1]['title'][:40]}"


@pytest.mark.parametrize("item", ALL, ids=_id)
def test_every_panel_says_what_empty_means(item):
    _board, p = item
    no_value = p.get("fieldConfig", {}).get("defaults", {}).get("noValue") or ""
    # a string check (ADR-007): it proves the text is specific, not that it is true
    assert len(no_value) > 10 and not no_value.lower().startswith("no data"), \
        f"{p['title']} shows only 'No data'"


@pytest.mark.parametrize("item", [i for i in ALL if i[1]["type"] == "timeseries"], ids=_id)
def test_every_series_has_a_legend_naming_its_labels(item):
    _board, p = item
    for t in p["targets"]:
        legend = t.get("legendFormat") or ""
        assert legend and legend != "__auto", f"{p['title']}: the legend would be the expression"
        by = re.search(r"\bby \(([^)]*)\)", t.get("expr", ""))
        for label in [x.strip() for x in by.group(1).split(",")] if by else []:
            if label != "le":
                assert "{{" + label + "}}" in legend, f"{p['title']}: legend omits {label}"


@pytest.mark.parametrize("item", [i for i in ALL if i[1]["type"] == "stat"], ids=_id)
def test_a_total_is_not_coloured_as_an_alarm(item):
    _board, p = item
    d = p.get("fieldConfig", {}).get("defaults", {})
    steps = (d.get("thresholds") or {}).get("steps")
    assert steps, f"{p['title']}: no thresholds means Grafana's default, red at 80"
    colours = {s["color"].lower() for s in steps} | {(d.get("color") or {}).get("fixedColor", "").lower()}
    assert not colours & ALARM, f"{p['title']} turns an alarm colour"


def test_no_expression_relies_on_absent():
    for board, p in ALL:
        for t in p.get("targets", []):
            assert "absent(" not in (t.get("expr") or ""), f"{board}/{p['title']}"


def _error_rate_expr() -> str:
    (p,) = [p for _b, p in ALL if p["title"] == "Error rate by skill"]
    return p["targets"][0]["expr"]


@pytest.mark.skipif(not docker_available(), reason="docker unavailable")
def test_a_skill_with_no_errors_has_an_error_rate_of_zero():
    from tests.stack_harness import stack
    with stack() as s:
        s.activation("skill", "e0-clean")
        s.activation("skill", "e0-mixed")
        s.activation("skill", "e0-mixed", error=True)
        s.wait_for_series('traces_span_metrics_calls_total{std_skill_name="e0-mixed",status_code="STATUS_CODE_ERROR"}')
        time.sleep(3)
        result = s.query(_error_rate_expr().replace("[1h]", "[5m]"))["result"]
        got = {r["metric"]["std_skill_name"]: float(r["value"][1]) for r in result}
    assert got.get("e0-clean") == 0, f"a clean skill must read 0, not vanish: {got}"
    assert 0 < got.get("e0-mixed", 0) < 1, got
