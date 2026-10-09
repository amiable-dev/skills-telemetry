"""`native coverage`: which sessions is Claude Code not exporting for? (ADR-015 decision 2)

A session reads its telemetry settings once, at start, so one started before
native telemetry was on never exports, and resuming it changes nothing. Its
stdtel hooks still run, so its state file is the evidence that it exists; an
absence of `api_request` events in Loki for its session id is the evidence that
it is not exporting.

The judge is pure, and the reads are injected, so every outcome is testable
without a live Loki. Stdlib and PyYAML only: doctor must run without the
warehouse extras.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from stdtel.doctor import FAIL, PASS, PENDING, UNKNOWN, Check

NAME = "native coverage"
WINDOW_S = 24 * 3600      # sessions whose state was written this recently are judged
GRACE_S = 600             # a first completed turn younger than this may not have reached Loki
QUERY_TIMEOUT_S = 10
BATCH = 50                # session ids per Loki query, so no query outgrows a URL
#: What a Claude Code session id looks like. Ids go into a quoted LogQL regex, so
#: anything else is refused rather than escaped: it is not a session.
ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_MAX_TS = 1e11            # year ~5138: past this, time.localtime() can raise
HARNESS = "claude-code"
REMEDY = ("restart it; resuming is not enough. A session reads its telemetry settings once, at "
          "start, so one begun before native telemetry was on never exports")


@dataclass(frozen=True)
class Session:
    session_id: str
    harness: str
    repo: str
    started_at: float
    first_turn_at: float | None     # start of its first completed turn; None until a Stop records it
    written_at: float               # state file mtime


def _ts(v, optional: bool = False) -> float | None:
    if v is None and optional:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < _MAX_TS:
        raise ValueError(f"not a timestamp: {v!r}")
    return float(v)


def _session(stem: str, raw, written: float) -> Session:
    """One state file's session, or ValueError when any field is not what it must be."""
    if not ID_RE.fullmatch(stem) or not isinstance(raw, dict):
        raise ValueError("not a session")
    res = raw.get("resource")
    res = {} if res is None else res
    if not isinstance(res, dict):
        raise ValueError("resource is not an object")
    harness, repo = res.get("std.harness") or HARNESS, res.get("std.repo") or ""
    if not isinstance(harness, str) or not isinstance(repo, str):
        raise ValueError("harness or repo is not text")
    return Session(stem, harness, repo, _ts(raw.get("started_at") or 0.0),
                   _ts(raw.get("first_turn_at"), optional=True), _ts(written))


def read_states() -> list[Session]:
    """Every state file that holds a well-formed session. A half-written or
    malformed one is skipped alone: one bad file never sinks the check."""
    from stdtel.doctor import _state_files
    out = []
    for f in _state_files():
        try:
            out.append(_session(f.stem, json.loads(f.read_text()), f.stat().st_mtime))
        except (OSError, ValueError):
            continue
    return out


# --- Loki ------------------------------------------------------------------------------------

#: Largest first: Prometheus's grammar, which Loki uses, takes each unit once, in this order.
_UNITS = {"y": 365 * 86400, "w": 7 * 86400, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def duration_s(text: str) -> float:
    """A Loki/Prometheus duration: `30d`, `720h`, `2h0m0s`, `1w`."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|[smhdwy])", str(text))
    order = [list(_UNITS).index(u) for _, u in parts]
    if (not parts or "".join(n + u for n, u in parts) != str(text).strip()
            or order != sorted(set(order))):
        raise ValueError(f"not a duration: {text!r}")
    return sum(float(n) * _UNITS[u] for n, u in parts)


def retention_from_config(cfg: dict) -> float | None:
    """The shortest retention the compactor enforces: `limits_config.retention_period`
    or any `retention_stream` period, since a stream rule may be the one covering
    Claude Code's events. None means kept for ever: zero, or not enforced.
    Per-tenant runtime overrides are not read; this stack runs one tenant, no overrides."""
    if not (cfg.get("compactor") or {}).get("retention_enabled"):
        return None
    limits = cfg.get("limits_config") or {}
    periods = [duration_s(limits.get("retention_period") or "0s"),
               *(duration_s(r.get("period") or "0s") for r in limits.get("retention_stream") or [])]
    return min((p for p in periods if p), default=None)


def _loki() -> str:
    return os.environ.get("STDTEL_LOKI", "http://localhost:11010").rstrip("/")


def _loki_shown() -> str:
    """Loki's URL as printed: never with credentials in it."""
    u = urllib.parse.urlsplit(_loki())
    return urllib.parse.urlunsplit(u._replace(netloc=u.hostname + (f":{u.port}" if u.port else ""))) \
        if u.hostname else "STDTEL_LOKI"


def read_retention() -> float | None:
    import yaml
    with urllib.request.urlopen(f"{_loki()}/config", timeout=QUERY_TIMEOUT_S) as r:
        return retention_from_config(yaml.safe_load(r.read().decode()) or {})


def observed_query(ids: list[str], range_s: int) -> str:
    """One grouped count, bounded to the sessions being judged."""
    bad = [i for i in ids if not ID_RE.fullmatch(i)]
    if bad:
        raise ValueError(f"{len(bad)} id(s) are not session ids")
    alt = "|".join(re.escape(i) for i in ids).replace("\\", "\\\\")
    return (f'sum by (session_id) (count_over_time({{service_name="{HARNESS}"}} '
            f'| event_name="api_request" | session_id=~"{alt}" [{range_s}s]))')


def parse_observed(body: dict) -> set[str]:
    if body.get("status") != "success":
        raise RuntimeError(f"Loki query failed: {body.get('error') or body.get('status')}")
    return {r["metric"].get("session_id", "") for r in body["data"]["result"]
            if float(r["value"][1]) > 0} - {""}


def _fetch(query: str, now: float) -> dict:
    q = urllib.parse.urlencode({"query": query, "time": str(int(now))})
    with urllib.request.urlopen(f"{_loki()}/loki/api/v1/query?{q}", timeout=QUERY_TIMEOUT_S) as r:
        return json.loads(r.read().decode())


def read_observed(ids: list[str], since: float, fetch=None, now: float | None = None) -> set[str]:
    """Which of `ids` have any api_request since `since`, BATCH ids per query."""
    now = time.time() if now is None else now
    fetch = fetch or (lambda q: _fetch(q, now))
    range_s = max(60, int(now - since))
    seen: set[str] = set()
    for i in range(0, len(ids), BATCH):
        seen |= parse_observed(fetch(observed_query(ids[i:i + BATCH], range_s)))
    return seen


# --- the judge -------------------------------------------------------------------------------

def population(sessions: list[Session], now: float) -> list[Session]:
    """Claude Code sessions written within the window that have completed a turn."""
    return [x for x in sessions
            if x.harness == HARNESS and x.first_turn_at and now - x.written_at <= WINDOW_S]


def _name(x: Session) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(x.started_at)) if x.started_at else "unknown start"
    return f"{x.session_id} ({x.repo or 'unknown project'}, started {when})"


def judge(sessions: list[Session], observed: set[str], retention_s: float | None, now: float) -> Check:
    """Outcome precedence: any missing fails; else any pending; else any unknown; else pass.
    The whole check is unknown when Loki keeps less than the window, and pending
    when there is nobody to judge (an empty population is not a pass)."""
    pop = population(sessions, now)
    if retention_s is not None and retention_s < WINDOW_S:
        return Check(NAME, UNKNOWN, f"Loki retention is {retention_s / 3600:g} h, shorter than the "
                     f"{WINDOW_S // 3600} h window, so an absent record proves nothing",
                     "raise limits_config.retention_period in Loki's config (deploy/loki.yaml keeps 720h)")
    if not pop:
        return Check(NAME, PENDING, f"no Claude Code session completed a turn in the last {WINDOW_S // 3600} h",
                     "nothing to judge yet: run a session and check again")
    missing, pending, unknown = [], [], []
    for x in pop:
        if x.session_id in observed:
            continue
        if now - x.first_turn_at < GRACE_S:
            pending.append(x)
        elif retention_s is not None and now - x.first_turn_at > retention_s:
            unknown.append(x)
        else:
            missing.append(x)
    seen = len(pop) - len(missing) - len(pending) - len(unknown)
    summary = f"{seen} of {len(pop)} session(s) exporting"
    if missing:
        return Check(NAME, FAIL, f"{summary}; not exporting: " + "; ".join(map(_name, missing)), REMEDY)
    if pending:
        return Check(NAME, PENDING, f"{summary}; inside the {GRACE_S // 60}-minute grace period: "
                     + "; ".join(map(_name, pending)), "check again in a few minutes")
    if unknown:
        return Check(NAME, UNKNOWN, f"{summary}; first turn older than Loki's retention, so absence "
                     "proves nothing: " + "; ".join(map(_name, unknown)),
                     "nothing to do unless it is still running; if it is, restart it")
    return Check(NAME, PASS, summary)


def native_coverage(states=read_states, observed=read_observed, retention=read_retention,
                    now: float | None = None) -> Check:
    now = time.time() if now is None else now
    try:
        sessions = states()
    except Exception as e:                        # noqa: BLE001 - a diagnostic must not raise
        return Check(NAME, UNKNOWN, f"cannot read session state: {e}", "check STDTEL_STATE_DIR")
    pop = population(sessions, now)
    try:
        keep = retention()
        if not pop or (keep is not None and keep < WINDOW_S):
            return judge(sessions, set(), keep, now)
        oldest = min(x.first_turn_at for x in pop)
        since = max(oldest - 60, now - keep) if keep is not None else oldest - 60
        seen = observed(sorted(x.session_id for x in pop), since)
    except Exception as e:                        # noqa: BLE001
        return Check(NAME, UNKNOWN, f"cannot read Loki ({_loki_shown()}): {type(e).__name__}",
                     "start the stack (`make up`), or point STDTEL_LOKI at Loki's query API")
    return judge(sessions, seen, keep, now)
