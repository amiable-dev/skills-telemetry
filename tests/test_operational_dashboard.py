"""#140: the operational dashboard showed nothing about usage during a live
session that spent $15.43 over 167 requests.

Three faults, each now a test: no panel read Loki, where Claude Code's own
per-request records land (ADR-014); one panel read a native metric the collector
drops by design (#110), so it could never show anything; and "/ h" panels
plotted per-second rates.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BOARDS = ROOT / "deploy" / "grafana" / "provisioning" / "dashboards"
OPERATIONAL = json.loads((BOARDS / "skill-scorecard.json").read_text())
assert OPERATIONAL["uid"] == "stdtel-operational"
COLLECTOR = yaml.safe_load((ROOT / "collector" / "otel-collector.yaml").read_text())


def targets(board):
    for p in board["panels"]:
        for t in p.get("targets", []):
            yield p, t


def loki_targets(board):
    for p, t in targets(board):
        ds = t.get("datasource") or p.get("datasource") or {}
        if isinstance(ds, dict) and ds.get("uid") == "Loki":
            yield p, t


# --- the dead panel, and the class of it ---------------------------------------------------------

def _dropped_metric_prefixes() -> list[str]:
    """What filter/native_metrics drops, as Prometheus spells it (dots become underscores)."""
    conds = COLLECTOR["processors"]["filter/native_metrics"]["metrics"]["metric"]
    pats = [re.search(r'"\^([^"]+)"', c).group(1).replace("\\", "").replace(".", "_") for c in conds]
    assert pats, "could not read the collector's native-metric filter"
    return pats


def test_no_panel_queries_a_metric_the_collector_drops():
    prefixes = _dropped_metric_prefixes()
    assert "claude_code_" in prefixes
    for board in BOARDS.glob("*.json"):
        for p, t in targets(json.loads(board.read_text())):
            expr = t.get("expr") or ""
            for pre in prefixes:
                assert not re.search(rf"\b{pre}\w+", expr), \
                    f"{board.name}/{p['title']} reads {pre}*, which the collector drops (#110)"


# --- per hour means per hour ---------------------------------------------------------------------

def test_a_per_hour_panel_plots_per_hour():
    """rate() is per second whatever its window; "/ h" needs * 3600 or increase()."""
    checked = 0
    for board in BOARDS.glob("*.json"):
        for p, t in targets(json.loads(board.read_text())):
            expr = t.get("expr") or ""
            if "/ h" in p["title"] and "rate(" in expr and "histogram_quantile" not in expr:
                checked += 1
                assert "* 3600" in expr or "*3600" in expr, f"{p['title']} plots per second"
    assert checked >= 5


# --- usage, from the harness's own records -------------------------------------------------------

NEEDED = {"spend in range": "unwrap cost_usd", "requests in range": "count_over_time",
          "spend / h": "unwrap cost_usd", "tokens / h by type": "unwrap cache_read_tokens",
          "requests / h by model": "by (model)", "spend by skill, agent and repo": "skill_name"}


@pytest.mark.parametrize("title,needle", sorted(NEEDED.items()))
def test_the_dashboard_shows_usage_from_loki(title, needle):
    panels = [(p, t) for p, t in loki_targets(OPERATIONAL) if title in p["title"].lower()]
    assert panels, f"no Loki panel titled like {title!r}"
    exprs = " ".join(t["expr"] for _p, t in panels)
    assert 'event_name="api_request"' in exprs and needle in exprs


def test_usage_panels_say_where_the_numbers_come_from():
    """Native records exist only where Claude Code's telemetry is on; an empty
    panel must not read as zero spend."""
    for p, _t in loki_targets(OPERATIONAL):
        text = (p.get("description") or "").lower()
        assert "claude code" in text and ("stdtel-install" in text or "native" in text), p["title"]


def test_empty_skill_panels_say_why():
    for p in OPERATIONAL["panels"]:
        if "skill" in p["title"].lower() and "spend" not in p["title"].lower():
            no_value = (p.get("fieldConfig", {}).get("defaults", {}).get("noValue") or "").lower()
            assert "no skill" in no_value, f"{p['title']} says only 'No data'"


# --- every Loki query runs against Loki ----------------------------------------------------------

def _loki_up() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11010/ready", timeout=2).read()
        return True
    except Exception:                                   # noqa: BLE001
        return False


@pytest.mark.skipif(not _loki_up(), reason="Loki not running")
@pytest.mark.parametrize("title,expr", [(p["title"], t["expr"]) for p, t in loki_targets(OPERATIONAL)])
def test_every_loki_panel_query_runs(title, expr):
    q = expr.replace("$__range", "3h").replace("$__auto", "5m").replace("$__interval", "5m")
    url = "http://127.0.0.1:11010/loki/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": int(time.time())})
    body = json.loads(urllib.request.urlopen(url, timeout=10).read())
    assert body["status"] == "success", (title, body)
