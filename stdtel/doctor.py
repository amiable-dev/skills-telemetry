"""`stdtel doctor` — make a silently-broken install visible.

Hooks exit 0 so telemetry never blocks the developer. The cost of that rule is
that a broken install looks exactly like a working one: no spans and a healthy
session are indistinguishable from the outside. Four install-time failures in
this project's history were found by a person using it, none by its tests.

Every check reports a verdict, what was observed, and **the remedy** — the part a
diagnostic usually leaves out.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 3


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str = ""


def _git_branch() -> str:
    """Current branch, or empty when git cannot answer."""
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:                             # noqa: BLE001 - not a git repo, no git, etc.
        return ""


def hook_resolvable() -> Check:
    """Can a hook actually run `stdtel-hook`?

    Checked the way a hook resolves it — a non-login `sh -c` — not the way an
    interactive shell does. Shipping the bare name failed exactly here, with
    "command not found" on every tool call (#13), while it resolved fine in a
    terminal because a version manager had it on PATH.
    """
    try:
        # /bin/sh by absolute path: a diagnostic for a broken PATH must not need
        # a working PATH to run, and /bin/sh is what a hook is given anyway
        r = subprocess.run(["/bin/sh", "-c", "command -v stdtel-hook"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        found = r.stdout.strip()
    except Exception as e:                        # noqa: BLE001 - diagnostics must not raise
        return Check("hook resolvable", False, f"could not probe: {e}",
                     "check that `sh` is available")
    if found:
        return Check("hook resolvable", True, f"sh -c finds {found}")
    which = shutil.which("stdtel-hook")
    detail = ("not on the PATH a hook gets (sh -c); "
              + (f"an interactive shell finds {which}" if which else "not on this shell's PATH either"))
    return Check("hook resolvable", False, detail,
                 "uv tool install stdtel, then `stdtel-install settings` to write an absolute path")


def hooks_registered() -> Check:
    """Registered once, in exactly one place.

    Plugin hooks and settings hooks do not deduplicate against each other, so
    both firing means every window is counted twice.
    """
    import json
    settings = Path.home() / ".claude" / "settings.json"
    in_settings = False
    if settings.is_file():
        try:
            # Look for OUR hooks specifically. Testing `bool(hooks)` reported
            # "registered in settings AND as a plugin" to anyone with hooks of
            # their own — a confidently wrong warning, in the tool meant to
            # diagnose confidently wrong behaviour.
            raw = json.loads(settings.read_text() or "{}").get("hooks") or {}
            in_settings = "stdtel-hook" in json.dumps(raw)
        except json.JSONDecodeError:
            return Check("hooks registered", False, f"{settings} is not valid JSON",
                         "fix or remove the file, then run `stdtel-install settings`")
    plugin = (Path.home() / ".claude" / "plugins" / "installed_plugins.json")
    in_plugin = plugin.is_file() and "stdtel" in plugin.read_text()
    if in_settings and in_plugin:
        return Check("hooks registered", False, "registered in settings AND as a plugin",
                     "remove the hooks block from ~/.claude/settings.json; plugin hooks and settings "
                     "hooks both fire, double-counting every skill window")
    if in_settings or in_plugin:
        return Check("hooks registered", True,
                     "via settings.json" if in_settings else "via the plugin")
    return Check("hooks registered", False, "no stdtel hooks found",
                 "run `stdtel-install settings`, or install the plugin")


def branch_identity() -> Check:
    """Can this work be joined to a change request? (ADR-013)

    Any branch name joins, so this fails only where the join is impossible: no
    branch readable, or no remote to identify the repository. Both are only
    fixable now — nothing done later attributes work already recorded.
    """
    from stdtel.enrich import branch_hash, remote_url, repo_id
    branch = os.environ.get("STDTEL_BRANCH") or _git_branch()
    repo = repo_id(remote_url())
    if not branch or branch == "HEAD":
        return Check("branch identity", False, "no branch readable (not a git repository, or detached)",
                     "run Claude Code inside the repository's working tree, on a branch")
    if not repo:
        return Check("branch identity", False, f"branch {branch!r} but no remote",
                     "add a remote (`git remote add origin <url>`): the repository identity comes "
                     "from it, and without one no work here can join a change request")
    note = (" — work committed straight to it has no change request to join"
            if branch in ("main", "master") else "")
    return Check("branch identity", bool(branch_hash(repo, branch)), f"{branch} in {repo}{note}")


def catalogue_ok() -> Check:
    """Can the catalogue be found, and does it contain anything?

    An empty or unfindable catalogue does not fail loudly: skills are simply
    recorded as `unversioned`, with no standard_id and no policy_ids.
    """
    from stdtel.hooks.cli import _catalogue, overlay_roots, skills_roots
    roots = skills_roots()
    cat = _catalogue()
    if not roots:
        return Check("skill catalogue", False, "no catalogue root exists",
                     "symlink skills into ~/.claude/skills, or set STDTEL_SKILLS_ROOT")
    if not cat:
        return Check("skill catalogue", False,
                     f"{len(roots)} root(s) searched, 0 skills found",
                     "skills will record as `unversioned` with no standard_id; check "
                     "STDTEL_SKILLS_ROOT and run `stdtel-validate` on it")
    detail = f"{len(cat)} skill(s) across {len(roots)} root(s)"
    unversioned = sum(1 for m in cat.values() if m.version == "unversioned")
    if unversioned:
        # Not a failure — a skill can be used without being onboarded — but it is
        # the reason a scorecard row is missing, and it is invisible otherwise.
        detail += f", {unversioned} unversioned"
    overlays = overlay_roots()
    if overlays:
        attributed = sum(1 for m in cat.values() if m.overlay_path is not None)
        detail += f"; {attributed} attributed by overlay across {len(overlays)} overlay root(s)"
    return Check("skill catalogue", True, detail)


def scope_adoption() -> Check:
    """How much of the catalogue declares a unit of work, and how much we guessed.

    Never a failure. ADR-011 requires nothing of any author: an artefact that
    declares no scope is turn-scoped and still measured. But the ratio bounds
    every containment rollup ADR-010 produces — a run attributed to a skill is
    only as good as the declaration behind it, and a declaration supplied by an
    overlay is a local claim about somebody else's artefact rather than something
    that artefact said about itself.
    """
    from stdtel.hooks.cli import _agent_catalogue, _catalogue

    cat = _catalogue()
    agents = _agent_catalogue()
    if not cat and not agents:
        return Check("scope declarations", True,
                     "no catalogue to read; nothing is scoped and nothing needs to be")
    # Skills only. An agent cannot open a container yet (ADR-011), so counting a
    # decorated agent here would report adoption that changes no span.
    scoped = [m for m in cat.values() if m.scope]
    if not scoped:
        return Check("scope declarations", True,
                     f"0 of {len(cat)} skill(s) declare a scope — every "
                     f"activation is attributed to its turn",
                     "if a skill drives work across many turns, add `telemetry.scope: branch` "
                     "so its cost can be rolled up; see skills/stdtel-onboard")
    assumed = sum(1 for m in scoped if m.overlay_path is not None)
    detail = f"{len(scoped)} of {len(cat)} skill(s) declare a scope"
    if assumed:
        detail += (f"; {assumed} supplied by an overlay — an assertion about someone "
                   f"else's artefact, not something it states itself")
    detail += f"; {len(agents)} agent(s) in the catalogue"
    return Check("scope declarations", True, detail)


def collector_ok() -> Check:
    """Is anything listening where spans are being sent?

    A dead collector is the quietest failure of all: the exporter times out and
    drops, leaving one stderr line no editor shows.
    """
    import urllib.error
    import urllib.request
    from stdtel.exporter import _endpoint
    endpoint = _endpoint()
    req = urllib.request.Request(endpoint, data=b"{}", method="POST",
                                 headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_S)
        return Check("collector reachable", True, f"{endpoint} answered")
    except urllib.error.HTTPError:
        # any HTTP status means something is listening and speaking OTLP
        return Check("collector reachable", True, f"{endpoint} answered")
    except Exception as e:                        # noqa: BLE001
        return Check("collector reachable", False, f"{endpoint}: {type(e).__name__}",
                     "spans are being dropped right now. Start the stack with `make up`, or point "
                     "STDTEL_OTLP_ENDPOINT at a live collector (OTEL_* cannot reach a hook)")


def _project_slug(path: Path) -> str:
    """Claude Code names a project directory after its path, with / as -."""
    return str(path).replace("/", "-")


def _owning_project(session_id: str) -> str | None:
    """Which project's transcript directory holds this session, if any."""
    try:
        for d in (Path.home() / ".claude" / "projects").iterdir():
            if (d / f"{session_id}.jsonl").exists():
                return d.name
    except Exception:                             # noqa: BLE001 - a diagnostic must never crash
        pass
    return None


def recent_state() -> Check:
    """Has a hook run recently *for this project*? Registration proves nothing.

    Neither does state from somewhere else, which is how #44 presented: every
    state file on the machine belonged to other projects, the session being
    watched had none, and this check said `ok hooks running`. True, and the
    opposite of useful.

    Claude Code reads hook configuration at session start, so a session that was
    already open when stdtel was installed runs no hooks for its entire life.
    That is the single most common reason for an empty dashboard, and it is
    invisible unless the check knows whose data it found.
    """
    import datetime as dt

    from stdtel.state import state_dir
    try:
        files = sorted(state_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:                        # noqa: BLE001
        return Check("hooks running", False, f"cannot read state dir: {e}", "check STDTEL_STATE_DIR")
    if not files:
        return Check("hooks running", False, "no session state has ever been written",
                     "the hooks are registered but not firing; run `stdtel-install where` and "
                     "invoke stdtel-hook by hand to see the error")

    when = lambda f: f"{dt.datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}"
    here = _project_slug(Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()))
    owners = {f: _owning_project(f.stem) for f in files}
    mine = [f for f in files if owners[f] == here]
    if mine:
        return Check("hooks running", True, f"last session state {when(mine[0])}, from this project")
    if all(o is None for o in owners.values()):
        # Copilot writes no Claude transcript, so ownership can be unknowable.
        # Unknown is not wrong — but it must not be reported as "from here".
        return Check("hooks running", True, f"last session state {when(files[0])}, project unknown")
    elsewhere = next(o for o in owners.values() if o)
    return Check("hooks running", False,
                 f"newest state {when(files[0])} came from {elsewhere.lstrip('-')}, "
                 f"nothing from this project",
                 "hook configuration is read at session start, so a session already open when "
                 "stdtel was installed never picks it up — restart Claude Code in this project")


def plugin_in_step() -> Check:
    """Plugin and package are two installs, and neither updates the other.

    Not having the plugin is fine — `stdtel-install settings` is a supported
    install. Having a *stale* one is not: the hooks it registers still work, so
    nothing breaks, while the skills it ships quietly lag the release.
    """
    import stdtel
    from stdtel.plugin import installed_plugin, skew_notice

    found = installed_plugin()
    if found is None:
        return Check("plugin in step", True,
                     f"package {stdtel.__version__}; no plugin installed")
    marketplace, plugin_version = found
    notice = skew_notice(stdtel.__version__)
    if not notice:
        return Check("plugin in step", True,
                     f"plugin and package both {plugin_version} ({marketplace})")
    return Check("plugin in step", False,
                 f"plugin {plugin_version}, package {stdtel.__version__}",
                 notice.split("run: ", 1)[1] if "run: " in notice else notice)


def artefacts_observed() -> Check:
    """Which ADR-009 events have actually fired on this machine?

    Registration is not observation. The three newest hook events are the ones
    that capture the largest unmeasured costs — sub-agent runs and compaction —
    and a settings file that mentions them proves only that somebody edited a
    settings file. Until one has fired, the payload shapes stdtel codes against
    are documentation, not evidence, and this check says so rather than letting
    silence read as success.
    """
    import json

    from stdtel.state import state_dir

    wanted = {"subagent-start", "subagent-stop", "post-compact"}
    seen: set[str] = set()
    try:
        files = sorted(state_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception as e:                        # noqa: BLE001
        return Check("artefact events", False, f"cannot read state dir: {e}", "check STDTEL_STATE_DIR")
    for f in files[:50]:
        try:
            seen |= set(json.loads(f.read_text()).get("observed_events") or [])
        except Exception:                         # noqa: BLE001 - a half-written state file
            continue
    missing = sorted(wanted - seen)
    if not missing:
        return Check("artefact events", True, "sub-agent and compaction capture both observed")
    if not files:
        return Check("artefact events", False, "no session state written yet",
                     "start a session with the hooks installed")
    return Check("artefact events", False,
                 f"not yet observed: {', '.join(missing)}",
                 "expected until a session runs with these hooks registered AND spawns a "
                 "sub-agent or compacts. Re-run `stdtel-install settings`, restart Claude Code, "
                 "then spawn a sub-agent. Until then those kinds fall back to reading the "
                 "transcript and are flagged std.artefact.source=transcript")


# --- ADR-014 decision 13: is content dropped on this machine? ----------------------------

#: Reserved by ADR-014 decision 13. The loaders skip it, so a probe never becomes a row.
PROBE_SERVICE = "stdtel-probe"
#: Content-shaped keys the probe carries the marker in: one Claude Code sends with
#: OTEL_LOG_TOOL_DETAILS, one its commands arrive in, and the prompt.
PROBE_CONTENT_KEYS = ("tool_input", "full_command", "prompt")
_CONTENT_REMEDY = ("content is reaching storage. Turn OTEL_LOG_TOOL_DETAILS off now, then restart the "
                   "collector with this repository's config (`make down && make up`) and run "
                   "`stdtel-doctor --content-check` again")


def judge_content_probe(tempo: tuple[bool, bool], loki: tuple[bool, bool]) -> Check:
    """Each argument is (control found, marker found) for one store.

    The control is what separates "dropped" from "never arrived": without it an
    empty store would pass.
    """
    name = "content dropped"
    leaked = [s for s, (_, marker) in (("Tempo", tempo), ("Loki", loki)) if marker]
    if leaked:
        return Check(name, False, f"the probe's content reached {' and '.join(leaked)}", _CONTENT_REMEDY)
    missing = [s for s, (control, _) in (("Tempo", tempo), ("Loki", loki)) if not control]
    if missing:
        return Check(name, False,
                     f"could not confirm: the probe never arrived in {' or '.join(missing)}, so an "
                     "absent marker proves nothing",
                     "start the stack with `make up` (Tempo on STDTEL_TEMPO, Loki on STDTEL_LOKI) and "
                     "run `stdtel-doctor --content-check` again. Keep OTEL_LOG_TOOL_DETAILS off until it passes")
    return Check(name, True, "the collector dropped the probe's content before Tempo and Loki")


def _probe_bodies(probe_id: str, marker: str) -> dict[str, dict]:
    import time
    import uuid

    def kv(d: dict) -> list:
        return [{"key": k, "value": {"stringValue": v}} for k, v in d.items()]

    ns = str(time.time_ns())
    resource = {"attributes": kv({"service.name": PROBE_SERVICE})}
    attrs = kv({"stdtel.probe.id": probe_id, **{k: marker for k in PROBE_CONTENT_KEYS}})
    return {
        "/v1/traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [{
            "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": "stdtel.probe",
            "kind": 1, "startTimeUnixNano": ns, "endTimeUnixNano": ns, "attributes": attrs}]}]}]},
        "/v1/logs": {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [{
            "timeUnixNano": ns, "body": {"stringValue": "stdtel content probe"},
            "attributes": [*attrs, *kv({"event.name": "stdtel_probe"})]}]}]}]},
    }


def _http_json(url: str, body: dict | None = None) -> str:
    import json
    import urllib.request
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read().decode()


def _send_probe(path: str, body: dict) -> None:
    from stdtel.exporter import _endpoint
    _http_json(_endpoint().removesuffix("/v1/traces") + path, body)


def _read_tempo(probe_id: str) -> str:
    """Everything Tempo holds for the probe's trace, found by the control id."""
    import json
    import urllib.parse
    base = os.environ.get("STDTEL_TEMPO", "http://localhost:3200").rstrip("/")
    q = urllib.parse.urlencode({"q": f'{{ span.stdtel.probe.id = "{probe_id}" }}', "limit": 5})
    found = json.loads(_http_json(f"{base}/api/search?{q}")).get("traces") or []
    return "".join(_http_json(f"{base}/api/traces/{t['traceID']}") for t in found)


def _read_loki(probe_id: str) -> str:
    import time
    import urllib.parse
    base = os.environ.get("STDTEL_LOKI", "http://localhost:11010").rstrip("/")
    now = time.time_ns()
    q = urllib.parse.urlencode({"query": f'{{service_name="{PROBE_SERVICE}"}}', "limit": 100,
                                "start": str(now - 600 * 10**9), "end": str(now + 10**9)})
    return _http_json(f"{base}/loki/api/v1/query_range?{q}")


def probe_content(send=None, read_tempo=None, read_loki=None, attempts: int = 30,
                  wait: float = 1.0) -> Check:
    """Send one span and one event holding a marker in content-shaped keys, then
    read both stores back. The stores are injectable so the verdict is testable."""
    import time
    import uuid
    send, read_tempo, read_loki = send or _send_probe, read_tempo or _read_tempo, read_loki or _read_loki
    probe_id, marker = f"probe-{uuid.uuid4().hex}", f"CONTENT-{uuid.uuid4().hex}"
    try:
        for path, body in _probe_bodies(probe_id, marker).items():
            send(path, body)
    except Exception as e:                        # noqa: BLE001
        return Check("content dropped", False, f"could not send the probe: {type(e).__name__}: {e}",
                     "start the collector (`make up`) and keep OTEL_LOG_TOOL_DETAILS off until this passes")

    seen = {"tempo": (False, False), "loki": (False, False)}
    for i in range(attempts):
        for store, read in (("tempo", read_tempo), ("loki", read_loki)):
            if seen[store][0]:
                continue
            try:
                text = read(probe_id)
            except Exception:                     # noqa: BLE001 - not there yet, or down: retried
                continue
            seen[store] = (probe_id in text, marker in text)
        if all(control for control, _ in seen.values()):
            break
        if i + 1 < attempts:
            time.sleep(wait)
    return judge_content_probe(**seen)


def _tool_details_enabled() -> bool:
    """Is OTEL_LOG_TOOL_DETAILS on, in the environment or Claude Code's settings?"""
    import json

    def on(v) -> bool:
        return str(v or "").strip().lower() not in ("", "0", "false", "no", "off")

    if on(os.environ.get("OTEL_LOG_TOOL_DETAILS")):
        return True
    files = [Path.home() / ".claude" / "settings.json"]
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    files += [Path(project) / ".claude" / f for f in ("settings.json", "settings.local.json")]
    for f in files:
        try:
            if on((json.loads(f.read_text()).get("env") or {}).get("OTEL_LOG_TOOL_DETAILS")):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def content_dropped() -> Check:
    """Only worth proving when something could send content: the detailed view
    is the one setting that makes Claude Code send it."""
    if not _tool_details_enabled():
        return Check("content dropped", True,
                     "detailed view off (OTEL_LOG_TOOL_DETAILS unset), so nothing sends content; "
                     "`--content-check` probes anyway")
    return probe_content()


CHECKS = (hook_resolvable, hooks_registered, plugin_in_step, branch_identity, catalogue_ok,
          scope_adoption, collector_ok, content_dropped, recent_state, artefacts_observed)


def check_all() -> list[Check]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:                    # noqa: BLE001 - a diagnostic must never crash
            out.append(Check(fn.__name__, False, f"check itself failed: {e}",
                             "this is a bug in stdtel doctor"))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="stdtel-doctor", description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", "-q", action="store_true", help="only show problems")
    ap.add_argument("--content-check", action="store_true",
                    help="only prove the collector drops content: send a probe and read Tempo and "
                         "Loki back (ADR-014 decision 13; run before enabling OTEL_LOG_TOOL_DETAILS)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    checks = [probe_content()] if a.content_check else check_all()
    failed = [c for c in checks if not c.ok]
    for c in checks:
        if c.ok and a.quiet:
            continue
        mark = "ok  " if c.ok else "FAIL"
        print(f"  {mark}  {c.name:<20} {c.detail}")
        if not c.ok and c.remedy:
            print(f"        -> {c.remedy}")
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
