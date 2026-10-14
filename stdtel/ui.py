"""`stdtel-ui`: a local health page beside Grafana (ADR-017).

The page owns what only this machine can see; Grafana owns the stores. It serves
doctor's own checks and nothing else, on loopback, in berth's shape:

* bound to 127.0.0.1 on one port: `--port`, else STDTEL_UI_PORT, else the berth
  `web` port (WEB_PORT). It never picks another, and a held port is a refusal;
* GET (and HEAD) only. The page is a string in this package, so nothing is served
  from disk. Every response carries a per-response CSP with `frame-ancestors 'none'`;
* the Host header must be exactly `127.0.0.1:<port>` or `localhost:<port>`, the
  defence against DNS rebinding. Anything else gets 421;
* the one JSON endpoint refuses cross-site requests;
* each check runs under its own timeout and is cached for a TTL. Concurrent requests
  share one run. A check that times out or raises is `unknown`, and the others still
  render;
* paths under the home directory render as `~`, and the value of a setting never
  renders: it is replaced by the setting's name.

A foreground process with no state of its own: Ctrl-C ends it.
"""
from __future__ import annotations

import concurrent.futures as cf
import errno
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from stdtel import doctor
from stdtel.ui_page import PAGE

HOST = "127.0.0.1"
#: Checks that query a store or the network; the rest read local files.
STORE_CHECKS = {"collector reachable", "content dropped", "native coverage", "warehouse freshness",
                "loader liveness"}
FILE_TTL_S, STORE_TTL_S = 15.0, 60.0
FILE_TIMEOUT_S, STORE_TIMEOUT_S = 5.0, 20.0
#: Settings whose values never render, replaced by the setting's name.
SETTING_PREFIXES = ("STDTEL_", "OTEL_", "CLAUDE_CODE_", "ANTHROPIC_")
#: Only a value shaped like one is redacted: a URL, path, DSN, header or long token.
#: A plain word (`skills`, `agent`) says nothing about the machine, and replacing it
#: wherever it appears turned `sub-agent` into `sub-$STDTEL_HARNESS_MODE` (live, #162).
_VALUE_SHAPED = re.compile(r"[/:@=%]|^\S{16,}$")
#: A value stands as a value when nothing word-like or path-like touches its start,
#: and nothing word-like touches its end (a path or URL may continue: `/v1/traces`).
_BEFORE, _AFTER = r"(?<![\w./:@=-])", r"(?![\w-])"


def _name(fn) -> str:
    return getattr(fn, "check_name", fn.__name__)


# --- what may be rendered --------------------------------------------------------------------------

def setting_values(environ=None, settings_files=None) -> dict[str, str]:
    """value -> the setting it came from, for every value that must not render: the
    environment's, and the `env` blocks of Claude Code's settings files."""
    environ = os.environ if environ is None else environ
    found: dict[str, str] = {}

    def add(k, v):
        v = str(v)
        if k.startswith(SETTING_PREFIXES) and len(v) >= 4 and _VALUE_SHAPED.search(v):
            found.setdefault(v, k)

    for k, v in environ.items():
        add(k, v)
    try:
        files = doctor._claude_settings_files() if settings_files is None else settings_files
    except Exception:                                   # noqa: BLE001 - redaction must not fail open below
        files = []
    for f in files:
        try:
            env = doctor._settings_env(Path(f))
        except Exception:                               # noqa: BLE001
            continue
        for k, v in (env or {}).items():
            add(k, v)
    return found


def redact(text: str, values: dict[str, str], home: str | None = None) -> str:
    """A setting's value becomes its name; the home directory becomes `~`. Longest
    values first, so a value that contains another is replaced whole."""
    for v in sorted(values, key=len, reverse=True):
        text = re.sub(_BEFORE + re.escape(v) + _AFTER, lambda _m, n=values[v]: f"${n}", text)
    home = home if home is not None else str(Path.home())
    if home and home != "/":
        text = re.sub(re.escape(home) + r"(?=/|\b|$)", "~", text)
    return text


def render(check: doctor.Check, values: dict[str, str], home: str | None = None) -> dict:
    d = check.as_json()
    return {k: redact(v, values, home) if isinstance(v, str) else v for k, v in d.items()}


# --- bounded, cached, coalesced execution ----------------------------------------------------------

class Runner:
    """Runs each check at most once at a time, caches its result for a TTL, and gives
    up waiting on it after a timeout. A timed-out run keeps its slot until it ends,
    so a hung check never piles up threads. Checks run on daemon threads: a hung one
    must not hold the process open after Ctrl-C (ADR-017 decision 1), and the page
    holds no state a cut-off check could lose."""

    def __init__(self, checks=None, clock=time.monotonic, ttl=None, timeout=None):
        self.checks = tuple(doctor.CHECKS if checks is None else checks)
        self.clock = clock
        self.ttl = ttl or (lambda name: STORE_TTL_S if name in STORE_CHECKS else FILE_TTL_S)
        self.timeout = timeout or (lambda name: STORE_TIMEOUT_S if name in STORE_CHECKS else FILE_TIMEOUT_S)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, doctor.Check]] = {}
        self._inflight: dict[str, tuple[float, cf.Future]] = {}

    def _run(self, fn) -> doctor.Check:
        name = _name(fn)
        try:
            c = fn()
        except Exception as e:                          # noqa: BLE001 - same rule as doctor.check_all
            c = doctor.Check(name, doctor.UNKNOWN, f"check itself failed: {e}", "this is a bug in stdtel doctor")
        with self._lock:
            self._cache[name] = (self.clock(), c)
            self._inflight.pop(name, None)
        return c

    def _future(self, fn) -> tuple[float, cf.Future]:
        name = _name(fn)
        with self._lock:
            cached = self._cache.get(name)
            if cached and self.clock() - cached[0] < self.ttl(name):
                f: cf.Future = cf.Future()
                f.set_result(cached[1])
                return cached[0], f
            if name not in self._inflight:
                f = cf.Future()
                threading.Thread(target=lambda: f.set_result(self._run(fn)), daemon=True,
                                 name=f"stdtel-ui {name}").start()
                self._inflight[name] = (self.clock(), f)
            return self._inflight[name]

    def results(self) -> list[doctor.Check]:
        pending = [(fn, *self._future(fn)) for fn in self.checks]
        out = []
        for fn, started, f in pending:
            name, limit = _name(fn), self.timeout(_name(fn))
            try:
                out.append(f.result(timeout=max(0.0, started + limit - self.clock())))
            except cf.TimeoutError:
                out.append(doctor.Check(name, doctor.UNKNOWN, f"timed out after {limit:g}s; still running",
                                        "the page shows it when it finishes; run stdtel-doctor for detail"))
        return out


# --- the server --------------------------------------------------------------------------------------

def csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'none'", f"script-src 'nonce-{nonce}'",
        f"style-src 'nonce-{nonce}' https://fonts.googleapis.com", "font-src https://fonts.gstatic.com data:",
        "connect-src 'self'", "img-src 'self' data:", "base-uri 'none'", "form-action 'none'",
        "frame-ancestors 'none'"])


COMMON = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
          "X-Frame-Options": "DENY"}


def make_handler(port: int, runner: Runner, values_fn=setting_values):
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "stdtel-ui"
        sys_version = ""

        def log_message(self, *a):                      # quiet: the terminal is the user's
            pass

        def _send(self, status: int, body: str, ctype: str, extra: dict | None = None):
            raw = body.encode()
            self.send_response(status)
            for k, v in {**COMMON, "Content-Type": ctype, "Content-Length": str(len(raw)), **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _refuse_method(self):
            self._send(405, "method not allowed", "text/plain", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _refuse_method

        def _same_origin(self) -> bool:
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{self.headers.get('Host', '')}"

        def do_GET(self):
            if self.headers.get("Host", "") not in allowed:
                return self._send(421, "misdirected request: this page answers only to "
                                       f"127.0.0.1:{port} or localhost:{port}", "text/plain")
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                nonce = secrets.token_urlsafe(16)
                return self._send(200, PAGE.replace("__NONCE__", nonce).replace("__PORT__", str(port)),
                                  "text/html; charset=utf-8", {"Content-Security-Policy": csp(nonce)})
            if path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if path == "/api/checks":
                if not self._same_origin():
                    return self._send(403, '{"error":"cross-origin request refused"}', "application/json")
                values = values_fn()
                body = json.dumps({"checks": [render(c, values) for c in runner.results()]})
                return self._send(200, body, "application/json; charset=utf-8")
            return self._send(404, "not found", "text/plain")

        do_HEAD = do_GET

    return Handler


class PortHeld(OSError):
    pass


def serve(port: int, runner: Runner | None = None, values_fn=setting_values) -> ThreadingHTTPServer:
    """Bind and return the server, not yet serving. A held port raises PortHeld:
    another port is never picked (berth's rule)."""
    runner = runner or Runner()
    try:
        srv = ThreadingHTTPServer((HOST, port), make_handler(port, runner, values_fn), bind_and_activate=False)
        srv.allow_reuse_address = False
        srv.server_bind()
        srv.server_activate()
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise PortHeld(f"port {port} is held") from e
        raise
    if port == 0:                                       # tests: the handler must know the real port
        srv.RequestHandlerClass = make_handler(srv.server_address[1], runner, values_fn)
    srv.daemon_threads = True
    return srv


def resolve_port(arg: int | None, environ=None) -> int | None:
    environ = os.environ if environ is None else environ
    if arg is not None:
        return arg
    for k in ("STDTEL_UI_PORT", "WEB_PORT"):
        v = environ.get(k, "").strip()
        if v.isdigit():
            return int(v)
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stdtel-ui", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, help="port to bind on 127.0.0.1 (default STDTEL_UI_PORT, else "
                                             "berth's WEB_PORT); never picked automatically")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    port = resolve_port(a.port)
    if port is None:
        print("stdtel-ui: no port. Pass --port, set STDTEL_UI_PORT, or run under berth "
              "(eval \"$(berth env --shell)\" exports WEB_PORT).", file=sys.stderr)
        return 2
    try:
        srv = serve(port)
    except PortHeld:
        print(f"stdtel-ui: port {port} is held, and another is never picked. Ask who holds it: "
              f"berth who {port}", file=sys.stderr)
        return 1
    print(f"stdtel-ui: http://127.0.0.1:{port}/  (Ctrl-C to stop)", flush=True)
    return run_until_interrupted(srv)


def run_until_interrupted(srv: ThreadingHTTPServer) -> int:
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
