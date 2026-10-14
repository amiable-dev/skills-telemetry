"""ADR-017 W2 (#163): deep links into Grafana, and one implementation.

- every link resolves to a provisioned dashboard uid, and every variable it sets is
  one the dashboard defines and a panel uses;
- identifiers are URL-encoded;
- a check registered in doctor appears on the page with no page code.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

import pytest

from stdtel import coverage, doctor, ui, ui_links

ROOT = Path(__file__).resolve().parent.parent
BOARDS = {json.loads(f.read_text())["uid"]: json.loads(f.read_text())
          for f in (ROOT / "deploy" / "grafana" / "provisioning" / "dashboards").glob("*.json")}
HOSTILE_ID = 'a b&var-x=1#frag/"<x>'


def _exprs(board) -> str:
    out = []
    for p in board["panels"]:
        for t in p.get("targets", []):
            out.append(t.get("expr") or t.get("rawSql") or "")
    return "\n".join(out)


def _every_link():
    for name in ui_links.LINKS:
        c = doctor.Check(name, "fail", "x", subjects=({"session": "sess-1", "since": "2026-10-14T10:00:00+00:00"},))
        yield from ui_links.links_for(c, grafana="http://localhost:3000")


ALL_LINKS = list(_every_link())


def test_there_are_links_to_check():
    assert len(ALL_LINKS) >= 3


@pytest.mark.parametrize("link", ALL_LINKS, ids=lambda l: l["label"])
def test_every_link_resolves_to_a_provisioned_dashboard_and_its_variables(link):
    u = urllib.parse.urlsplit(link["url"])
    m = re.fullmatch(r"/d/([\w-]+)", u.path)
    assert m and m.group(1) in BOARDS, f"no provisioned dashboard {u.path}"
    board = BOARDS[m.group(1)]
    defined = {v["name"] for v in board.get("templating", {}).get("list", [])}
    used = _exprs(board)
    for key, _value in urllib.parse.parse_qsl(u.query):
        if key.startswith("var-"):
            var = key[4:]
            assert var in defined, f"{board['uid']} defines no variable {var!r}"
            assert f"${var}" in used or "${" + var + "}" in used, f"no panel on {board['uid']} uses ${var}"
        elif key == "viewPanel":
            assert any(str(p.get("id")) == _value for p in board["panels"]), f"no panel {_value}"


def test_a_session_id_is_url_encoded_and_survives_the_round_trip():
    c = doctor.Check("native coverage", "fail", "x", subjects=({"session": "4122e12b-19.08", "since": "2026-10-14T10:00:00+00:00"},))
    (link,) = [l for l in ui_links.links_for(c, grafana="http://localhost:3000") if "var-session" in l["url"]]
    assert "%5C%5C." in link["url"], "the backslashes are URL-encoded"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(link["url"]).query)
    assert q["var-session"] == ["4122e12b-19\\\\.08"]
    assert q["from"] == ["1791972000000"], "the time range starts when the session did"


def test_an_id_that_is_not_a_session_id_gets_no_session_link():
    """Ids reach a quoted LogQL regex. Anything outside coverage.ID_RE could break out of
    the string, so no per-session link is made for it; the board link remains."""
    c = doctor.Check("native coverage", "fail", "x", subjects=({"session": HOSTILE_ID},))
    links = ui_links.links_for(c, grafana="http://localhost:3000")
    assert links and not any("var-session" in l["url"] for l in links)
    assert not any("var-x" in l["url"] or "#frag" in l["url"] for l in links)


def _loki_up() -> bool:
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:11010/ready", timeout=2).read()
        return True
    except Exception:                                   # noqa: BLE001
        return False


@pytest.mark.skipif(not _loki_up(), reason="Loki not running")
def test_every_loki_panel_parses_with_a_linked_session_substituted():
    """Grafana inserts the textbox value as typed into a double-quoted LogQL string.
    Python's re.escape writes `\\-`, which LogQL rejects as an invalid escape: the
    first version of these links opened broken panels."""
    import time
    import urllib.request
    c = doctor.Check("native coverage", "fail", "x", subjects=({"session": "4122e12b-1908.4008_96ec"},))
    (link,) = [l for l in ui_links.links_for(c, grafana="http://g") if "var-session" in l["url"]]
    value = urllib.parse.parse_qs(urllib.parse.urlsplit(link["url"]).query)["var-session"][0]
    for p in BOARDS["stdtel-operational"]["panels"]:
        for t in p.get("targets", []):
            if "$session" not in (t.get("expr") or ""):
                continue
            q = t["expr"].replace("$session", value).replace("$__range", "1h")
            url = "http://127.0.0.1:11010/loki/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": int(time.time())})
            body = json.loads(urllib.request.urlopen(url, timeout=10).read())
            assert body["status"] == "success", (p["title"], body)


def test_a_link_points_only_at_the_configured_grafana():
    c = doctor.Check("warehouse freshness", "fail", "x")
    for link in ui_links.links_for(c, grafana="http://grafana.internal:3000/"):
        assert link["url"].startswith("http://grafana.internal:3000/d/")


def test_a_check_with_no_aggregate_has_no_link():
    assert ui_links.links_for(doctor.Check("hook resolvable", "fail", "x"), grafana="http://g") == []


def test_every_linked_name_is_a_real_check():
    names = {getattr(fn, "check_name", fn.__name__) for fn in doctor.CHECKS}
    assert set(ui_links.LINKS) <= names, set(ui_links.LINKS) - names


# --- the session variable is real -----------------------------------------------------------------

def test_the_operational_board_filters_its_loki_panels_by_session():
    board = BOARDS["stdtel-operational"]
    (var,) = [v for v in board["templating"]["list"] if v["name"] == "session"]
    assert var["type"] == "textbox" and var["query"] == ".*", "the default matches every session"
    for p in board["panels"]:
        for t in p.get("targets", []):
            if "service_name=" in (t.get("expr") or ""):
                assert '| session_id=~"$session"' in t["expr"], p["title"]


# --- native coverage names its sessions structurally ----------------------------------------------

def test_native_coverage_carries_the_sessions_it_names():
    now = 1_800_000_000.0
    s = coverage.Session("sess-missing", "claude-code", "acme/app", now - 3600, now - 1800, now - 60)
    c = coverage.judge([s], observed=set(), retention_s=None, now=now)
    assert c.outcome == "fail"
    assert [x["session"] for x in c.subjects] == ["sess-missing"]
    assert c.subjects[0]["since"].startswith("2027-01-15T"), c.subjects


def test_subjects_stay_out_of_doctors_json_contract():
    c = doctor.Check("x", "pass", "y", subjects=({"session": "s"},))
    assert set(c.as_json()) == {"name", "outcome", "observed", "remedy", "observed_at"}


# --- one implementation --------------------------------------------------------------------------

def test_a_check_registered_in_doctor_appears_on_the_page_with_no_page_code(monkeypatch):
    @doctor.named("brand new check")
    def brand_new():
        return doctor.Check("brand new check", "pending", "added after the page was written")

    monkeypatch.setattr(doctor, "CHECKS", (brand_new,))
    srv = ui.serve(0)                                 # the default runner reads doctor.CHECKS
    try:
        import http.client
        import threading
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/checks", headers={"Host": f"127.0.0.1:{port}"})
        (got,) = json.loads(c.getresponse().read())["checks"]
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()
    assert got["name"] == "brand new check" and got["outcome"] == "pending" and got["links"] == []


def test_the_page_names_no_check():
    from stdtel.ui_page import PAGE
    for fn in doctor.CHECKS:
        assert getattr(fn, "check_name", fn.__name__) not in PAGE


def test_the_api_carries_links_and_the_page_renders_them_as_text_links():
    from stdtel.ui_page import PAGE
    c = doctor.Check("warehouse freshness", "fail", "x")
    r = ui.render(c, {}, home="/nonexistent")
    assert r["links"] and all(set(l) == {"label", "url"} for l in r["links"])
    script = PAGE[PAGE.index("<script"):]
    assert "c.links" in script and ".href =" in script
    assert "/^https?:" in script, "only http(s) hrefs"
