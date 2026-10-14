"""Deep links from the health page into Grafana (ADR-017 decision 4).

The page owns the machine; Grafana owns the stores. A finding with an aggregate
behind it links to the provisioned dashboard by uid. Where the finding names
sessions (`Check.subjects`), each link opens the operational board filtered to
that session, from when it started. Every identifier is URL-encoded, and written
as a regex literal that survives a LogQL string, because the panels match the
`session` variable with `=~`.

A check that is not listed here simply has no link: the page needs no code for it
(decision 5).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import urllib.parse

#: check name -> (dashboard uid, panel id or None, label, links per session). The uids
#: are the provisioned ones; tests/test_ui_links.py resolves every link against them.
#: Only a dashboard that defines the `session` variable gets per-session links.
LINKS: dict[str, tuple[str, int | None, str, bool]] = {
    "native coverage": ("stdtel-operational", None, "requests in Grafana", True),
    "artefact events": ("stdtel-operational", None, "activations in Grafana", True),
    "warehouse freshness": ("stdtel-scorecard", None, "loader freshness in Grafana", False),
    "loader liveness": ("stdtel-scorecard", None, "loader freshness in Grafana", False),
}
DEFAULT_RANGE = ("now-24h", "now")


def grafana_base(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return (environ.get("STDTEL_GRAFANA") or "http://localhost:3000").rstrip("/")


def _ms(iso: str) -> str | None:
    try:
        return str(int(dt.datetime.fromisoformat(iso).timestamp() * 1000))
    except (TypeError, ValueError):
        return None


#: A session id, as coverage.ID_RE defines one. Only these get a per-session link:
#: the id lands inside a double-quoted LogQL regex, so anything else could break out.
_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def session_regex(sid: str) -> str | None:
    """The id as a regex literal that survives a double-quoted LogQL string. Only `.` is
    special among the allowed characters, and LogQL needs its backslash doubled
    (re.escape's `\\-` is an invalid escape there). None for anything else."""
    if not _SESSION_ID.fullmatch(sid):
        return None
    return sid.replace(".", "\\\\.")


def _url(base: str, uid: str, params: list[tuple[str, str]]) -> str:
    return f"{base}/d/{urllib.parse.quote(uid, safe='')}?{urllib.parse.urlencode(params)}"


def links_for(check, grafana: str | None = None) -> list[dict]:
    """[{label, url}] for one check result; empty when it has no aggregate."""
    target = LINKS.get(check.name)
    if target is None:
        return []
    uid, panel, label, per_session = target
    base = (grafana or grafana_base()).rstrip("/")
    extra = [("viewPanel", str(panel))] if panel is not None else []
    out = []
    for s in (getattr(check, "subjects", ()) or ()) if per_session else ():
        sid = s.get("session")
        pattern = session_regex(sid) if isinstance(sid, str) else None
        if pattern is None:
            continue
        since = _ms(s.get("since", "")) or DEFAULT_RANGE[0]
        out.append({"label": f"session {sid}",
                    "url": _url(base, uid, [("from", since), ("to", "now"), ("var-session", pattern), *extra])})
    if not out:
        out.append({"label": label, "url": _url(base, uid, [("from", DEFAULT_RANGE[0]), ("to", DEFAULT_RANGE[1]), *extra])})
    return out
