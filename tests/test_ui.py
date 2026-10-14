"""ADR-017 W1 (#162): `stdtel-ui`, a local health page in berth's shape.

Each case the ticket names:
- Host validation, including a rebinding attempt, returning 421;
- GET only;
- the CSP header;
- cross-site refusal;
- port-held refusal;
- a check timing out rendered as `unknown` while the others render;
- concurrent requests coalescing to one run;
- a hostile check string rendered as text;
- `~` redaction;
- no setting value rendered.

The server runs for real on 127.0.0.1 (port 0 for the tests). The page's
text-only rendering is a string check over its script (ADR-007): no headless
browser here.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from stdtel import doctor, ui
from stdtel.ui_page import PAGE

HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


def check(name, outcome="pass", detail="ok", remedy="", fn_name=None):
    @doctor.named(name)
    def fn():
        return doctor.Check(name, outcome, detail, remedy)
    fn.__name__ = fn_name or name.replace(" ", "_")
    return fn


@pytest.fixture
def server():
    """Start a real server on a free port; yields (port, request)."""
    made = []

    def start(checks=None, values_fn=lambda: {}):
        runner = ui.Runner(checks=checks or [check("alpha")])
        srv = ui.serve(0, runner, values_fn)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        made.append(srv)
        port = srv.server_address[1]

        def request(method="GET", path="/", host=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h = {"Host": host if host is not None else f"127.0.0.1:{port}", **(headers or {})}
            c.request(method, path, headers=h)
            r = c.getresponse()
            body = r.read().decode()
            c.close()
            return r, body
        return port, request

    yield start
    for s in made:
        s.shutdown()
        s.server_close()


# --- Host validation ---------------------------------------------------------------------------------

def test_the_page_answers_to_its_two_names(server):
    port, req = server()
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        r, _ = req(host=host)
        assert r.status == 200, host


@pytest.mark.parametrize("host", ["attacker.example", "attacker.example:{port}", "127.0.0.1", "localhost",
                                  "127.0.0.1:1", "0.0.0.0:{port}", "[::1]:{port}", ""])
def test_any_other_host_is_421_including_a_rebinding_attempt(server, host):
    """A rebound name resolves to 127.0.0.1 but still sends its own Host."""
    port, req = server()
    for path in ("/", "/api/checks", "/healthz"):
        r, _ = req(path=path, host=host.format(port=port))
        assert r.status == 421, (host, path)


# --- GET only ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head(server, method):
    _port, req = server()
    r, _ = req(method=method, path="/api/checks")
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"


def test_head_has_headers_and_no_body(server):
    _port, req = server()
    r, body = req(method="HEAD")
    assert r.status == 200 and body == ""


# --- headers ------------------------------------------------------------------------------------------

def test_the_page_carries_a_per_response_csp_matching_its_nonce(server):
    _port, req = server()
    (r1, b1), (r2, _b2) = req(), req()
    policy = r1.getheader("Content-Security-Policy")
    assert "frame-ancestors 'none'" in policy and "default-src 'none'" in policy
    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    assert f'nonce="{nonce}"' in b1 and "__NONCE__" not in b1
    assert r2.getheader("Content-Security-Policy") != policy, "a fresh nonce per response"
    assert r1.getheader("X-Frame-Options") == "DENY" and r1.getheader("X-Content-Type-Options") == "nosniff"


def test_nothing_is_served_from_disk(server):
    _port, req = server()
    for path in ("/../pyproject.toml", "/stdtel/ui.py", "/static/x.js", "/etc/passwd"):
        r, _ = req(path=path)
        assert r.status == 404, path


# --- cross-site ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                                     {"Origin": "http://attacker.example"}, {"Origin": "null"}])
def test_the_json_endpoint_refuses_cross_site_requests(server, headers):
    _port, req = server()
    r, _ = req(path="/api/checks", headers=headers)
    assert r.status == 403


def test_the_json_endpoint_answers_its_own_page(server):
    port, req = server()
    r, body = req(path="/api/checks", headers={"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{port}"})
    assert r.status == 200 and json.loads(body)["checks"][0]["name"] == "alpha"


# --- port held ----------------------------------------------------------------------------------------

def test_a_held_port_is_refused_not_replaced():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(ui.PortHeld):
            ui.serve(port, ui.Runner(checks=[check("alpha")]))
    finally:
        s.close()


def test_main_says_who_to_ask_and_exits_nonzero_on_a_held_port(capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert ui.main(["--port", str(port)]) == 1
    finally:
        s.close()
    assert f"berth who {port}" in capsys.readouterr().err


def test_without_a_port_it_refuses_rather_than_picking_one(monkeypatch, capsys):
    monkeypatch.delenv("STDTEL_UI_PORT", raising=False)
    monkeypatch.delenv("WEB_PORT", raising=False)
    assert ui.main([]) == 2
    assert "--port" in capsys.readouterr().err


def test_the_port_comes_from_the_flag_then_the_setting_then_berth():
    assert ui.resolve_port(9, {"STDTEL_UI_PORT": "8", "WEB_PORT": "7"}) == 9
    assert ui.resolve_port(None, {"STDTEL_UI_PORT": "8", "WEB_PORT": "7"}) == 8
    assert ui.resolve_port(None, {"WEB_PORT": "7"}) == 7
    assert ui.resolve_port(None, {}) is None


def test_it_binds_loopback_only():
    srv = ui.serve(0, ui.Runner(checks=[check("alpha")]))
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


# --- bounded, cached, coalesced ------------------------------------------------------------------------

def test_a_check_that_times_out_is_unknown_and_the_others_render():
    gate = threading.Event()

    @doctor.named("slow")
    def slow():
        gate.wait(5)
        return doctor.Check("slow", "pass", "late")

    runner = ui.Runner(checks=[check("alpha"), slow, check("omega", "fail", "broken")],
                       timeout=lambda name: 0.3)
    t0 = time.monotonic()
    got = {c.name: c for c in runner.results()}
    gate.set()
    assert time.monotonic() - t0 < 2
    assert got["slow"].outcome == "unknown" and "timed out" in got["slow"].detail
    assert got["alpha"].outcome == "pass" and got["omega"].outcome == "fail"


def test_a_check_that_raises_is_unknown_with_the_reason():
    @doctor.named("boom")
    def boom():
        raise RuntimeError("disk on fire")
    (c,) = ui.Runner(checks=[boom]).results()
    assert c.outcome == "unknown" and "disk on fire" in c.detail


def test_concurrent_requests_share_one_run():
    runs = []

    @doctor.named("counted")
    def counted():
        runs.append(1)
        time.sleep(0.3)
        return doctor.Check("counted", "pass", "ok")

    runner = ui.Runner(checks=[counted])
    threads = [threading.Thread(target=runner.results) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1


def test_a_result_is_cached_for_its_ttl_then_rerun():
    runs, now = [], [100.0]

    @doctor.named("collector reachable")
    def store():
        runs.append(1)
        return doctor.Check("collector reachable", "pass", "ok")

    @doctor.named("hooks registered")
    def local():
        runs.append(2)
        return doctor.Check("hooks registered", "pass", "ok")

    runner = ui.Runner(checks=[store, local], clock=lambda: now[0])
    runner.results()
    now[0] += ui.FILE_TTL_S + 1                         # the file check is stale, the store check is not
    runner.results()
    assert runs.count(2) == 2 and runs.count(1) == 1
    now[0] += ui.STORE_TTL_S
    runner.results()
    assert runs.count(1) == 2


def test_a_hung_check_is_not_started_twice():
    gate, runs = threading.Event(), []

    @doctor.named("hung")
    def hung():
        runs.append(1)
        gate.wait(5)
        return doctor.Check("hung", "pass", "ok")

    runner = ui.Runner(checks=[hung], timeout=lambda name: 0.1)
    runner.results()
    runner.results()
    gate.set()
    assert len(runs) == 1


def test_the_page_shows_when_each_result_was_observed(server):
    _port, req = server()
    _r, body = req(path="/api/checks")
    (c,) = json.loads(body)["checks"]
    assert set(c) == {"name", "outcome", "observed", "remedy", "observed_at"}


# --- what is rendered -----------------------------------------------------------------------------------

def test_a_hostile_check_string_is_data_in_json_and_text_on_the_page(server):
    _port, req = server(checks=[check(HOSTILE, "fail", HOSTILE, HOSTILE, fn_name="hostile")])
    r, body = req(path="/api/checks")
    assert r.getheader("Content-Type").startswith("application/json")
    (c,) = json.loads(body)["checks"]
    assert c["observed"] == HOSTILE, "carried as data, unchanged"
    script = PAGE[PAGE.index("<script"):]
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in script, f"the page must never parse server text as HTML ({sink})"
    assert "textContent" in script


def test_paths_under_home_render_as_tilde(server):
    home = str(Path.home())
    _port, req = server(checks=[check("paths", "fail", f"{home}/.stdtel/sessions is unreadable",
                                      f"chmod u+r {home}/.stdtel")])
    _r, body = req(path="/api/checks")
    (c,) = json.loads(body)["checks"]
    assert home not in body
    assert c["observed"] == "~/.stdtel/sessions is unreadable" and c["remedy"] == "chmod u+r ~/.stdtel"


def test_a_home_prefix_inside_a_longer_name_is_left_alone():
    assert ui.redact("/Users/al/x and /Users/alice/y", {}, home="/Users/al") == "~/x and /Users/alice/y"


def test_no_setting_value_is_rendered(server, monkeypatch, tmp_path):
    secret = "postgresql://postgres:SECRET-MARKER-162@db:5432/stdtel"
    monkeypatch.setenv("STDTEL_DSN", secret)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.internal:4318")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer TOKEN-MARKER-162"}}))
    values_fn = lambda: ui.setting_values(settings_files=[settings])     # noqa: E731
    _port, req = server(checks=[check("echo", "fail",
                                      f"cannot reach {secret}; endpoint http://collector.internal:4318",
                                      "headers Authorization=Bearer TOKEN-MARKER-162")], values_fn=values_fn)
    _r, body = req(path="/api/checks")
    assert "SECRET-MARKER-162" not in body and "TOKEN-MARKER-162" not in body
    assert "collector.internal" not in body
    (c,) = json.loads(body)["checks"]
    assert "$STDTEL_DSN" in c["observed"], "the setting is named, its value is not shown"


def test_a_plain_word_setting_does_not_mangle_the_text():
    """Live run, 2026-10-14: STDTEL_SKILLS_ROOT=skills and STDTEL_HARNESS_MODE=agent
    turned `skills-telemetry` into `$STDTEL_SKILLS_ROOT-telemetry` and `sub-agent` into
    `sub-$STDTEL_HARNESS_MODE`. A plain word says nothing about the machine; only values
    shaped like one (a URL, path, DSN, header or long token) are redacted."""
    values = ui.setting_values({"STDTEL_SKILLS_ROOT": "skills", "STDTEL_HARNESS_MODE": "agent",
                                "STDTEL_TEAM": "platform"}, settings_files=[])
    text = "feat/x in github.com/amiable-dev/skills-telemetry; sub-agent and compaction; 16 skills"
    assert ui.redact(text, values, home="/nonexistent") == text


def test_a_value_is_replaced_only_where_it_stands_as_a_value():
    values = {"http://localhost:4318": "STDTEL_OTLP_ENDPOINT", "/opt/skills": "STDTEL_SKILLS_ROOT"}
    assert ui.redact("http://localhost:4318/v1/traces answered", values, home="/x") == \
        "$STDTEL_OTLP_ENDPOINT/v1/traces answered"
    assert ui.redact("roots: /opt/skills, /opt/skillset", values, home="/x") == \
        "roots: $STDTEL_SKILLS_ROOT, /opt/skillset"
    assert ui.redact("see http://localhost:43180", values, home="/x") == "see http://localhost:43180"


def test_trivial_values_are_not_treated_as_settings():
    assert ui.setting_values({"STDTEL_DISABLED": "1", "STDTEL_SPOOL": "true", "HOME": "/x"}, settings_files=[]) == {}


def test_the_real_checks_render_no_setting_value(monkeypatch, tmp_path):
    """The real doctor checks, with an endpoint setting that one of them echoes."""
    monkeypatch.setenv("STDTEL_OTLP_ENDPOINT", "http://127.0.0.1:9/MARKER-ENDPOINT")
    monkeypatch.setenv("STDTEL_STATE_DIR", str(tmp_path / "state"))
    runner = ui.Runner(checks=[doctor.collector_ok])
    out = json.dumps([ui.render(c, ui.setting_values(settings_files=[])) for c in runner.results()])
    assert "MARKER-ENDPOINT" not in out and "$STDTEL_OTLP_ENDPOINT" in out
