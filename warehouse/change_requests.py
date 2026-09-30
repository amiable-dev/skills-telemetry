"""The forge-neutral change-request record (ADR-013).

A change request is a pull request on GitHub and a merge request on GitLab. The
warehouse and every query read only the neutral shape defined here; each forge
has an adapter that fills it (`load_delivery.py` is GitHub's; GitLab is #103).

The join to capture is not a ticket. It is a branch identity — a hash of the
normalised repository and the branch, computed identically here and in
`stdtel.enrich` — plus a time window, confirmed by commit patch-ids. The ticket
is enrichment: the forge's own issue links first, then a fixed regex.

`warehouse/` imports nothing from `stdtel`, so `repo_id` and `branch_hash` exist
twice on purpose; `tests/test_change_requests.py` holds the two equal, because a
difference fails nothing and silently stops rows matching.
"""
from __future__ import annotations

import hashlib
import re

#: Must equal `stdtel.identity._ID_LENGTH`.
_ID_LENGTH = 16

_URL_REMOTE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", re.I)
_SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def _remote_parts(remote: str) -> tuple[str, str]:
    r = (remote or "").strip().rstrip("/")
    r = re.sub(r"\.git$", "", r).rstrip("/")
    m = _URL_REMOTE.match(r) or _SCP_REMOTE.match(r)
    return (m.group(1), m.group(2)) if m else ("", r)


def repo_id(remote: str) -> str:
    """`host/owner/name`, lower-cased. Identical to `stdtel.enrich.repo_id`."""
    host, path = _remote_parts(remote)
    if not path:
        return ""
    return f"{host}/{path}".lower() if host else path.lower()


def branch_hash(repo: str, branch: str) -> str:
    """Identical to `stdtel.enrich.branch_hash`: the ADR-013 join key."""
    if not repo or not branch:
        return ""
    return hashlib.sha256(f"stdtel-branch:{repo}\n{branch}".encode()).hexdigest()[:_ID_LENGTH]


def cr_id(forge: str, repo: str, number: int | str) -> str:
    return f"{forge}:{repo}!{number}"


# --- the ticket, as enrichment ------------------------------------------------------------

#: A key begins the text or a path segment and is not followed by a `.`: a
#: version continues with a dot and a ticket does not. `init-4.38.2` read as
#: INIT-4 on every CodeQL bump until this was added.
_KEY = re.compile(r"(?:^|[/\s(\[])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?![\d.])")

#: Bot branches carry no ticket by construction: their segments are package
#: names and versions, and a name with no dot after it (`types/node-24`) has
#: the shape of a key. Versions elsewhere — `release-0.6.0`, which read as
#: RELEASE-0 on both releases — are rejected by the dot rule above instead, so a
#: genuine key on a release branch (`release/v2/OPS-1234-rollback`) survives.
_BOT_BRANCHES = ("dependabot/", "renovate/")


#: Read case-insensitively, because Linear's branches are lowercase
#: (`chris/eng-123-title`). The cost is that a lowercase word and a number —
#: `fix/media-233` — reads as MEDIA-233. That is tolerable only because this is
#: enrichment: the forge's own link outranks it, and the join is on the branch.
def key_in(text: str) -> str | None:
    m = _KEY.search((text or "").upper())
    return m.group(1) if m else None


def key_in_branch(branch: str) -> str | None:
    if (branch or "").lower().startswith(_BOT_BRANCHES):
        return None
    return key_in(branch)


def ticket_links(forge: str, repo: str, closes: list, title: str, body: str,
                 branch: str) -> list[tuple[str, str]]:
    """[(ticket_id, source)] for one change request, in ADR-013's priority.

    The forge's own links are authoritative and are the only source when they
    exist. Otherwise one key, from the first place that has one: title, then
    body, then branch. The source is recorded so a query can trust the forge and
    discount the rest.
    """
    if closes:
        return [(f"{forge}:{repo}#{n}", "forge") for n in closes]
    for source, found in (("title", key_in(title)), ("body", key_in(body)),
                          ("branch", key_in_branch(branch))):
        if found:
            return [(found, source)]
    return []


# --- ADR-002's per-ticket rows, over change requests ------------------------------------------

def _hours(a, b):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 3)


def ticket_rows(crs: list[dict], links: list[dict], team: str) -> list[dict]:
    """One ticket row per ticket linked to any change request.

    Cycle time is first-opened to last-merged and is labelled a proxy wherever it
    surfaces (ADR-002 decision 6). A ticket whose change requests disagree on arm
    is `mixed`, for exclusion from crossover comparisons (decision 4). Closed,
    unmerged change requests count toward the arm but never toward done.
    """
    by_id = {c["cr_id"]: c for c in crs}
    groups: dict[str, list[dict]] = {}
    for link in links:
        c = by_id.get(link["cr_id"])
        if c is not None:
            groups.setdefault(link["ticket_id"], []).append(c)
    rows = []
    for ticket_id, group in groups.items():
        opened = min((c["opened_at"] for c in group if c["opened_at"]), default=None)
        merged = [c["merged_at"] for c in group if c["merged_at"]]
        done = max(merged) if merged else None
        arms = {c["assisted_by"] for c in group} - {"unknown"}
        rows.append({
            "ticket_id": ticket_id, "team": team, "story_points": None,
            "created_at": opened, "started_at": opened, "done_at": done,
            "cycle_time_hours": _hours(opened, done), "lead_time_hours": _hours(opened, done),
            "harness_arm": arms.pop() if len(arms) == 1 else "mixed" if arms else "unknown",
        })
    return rows


CR_COLS = ["cr_id", "forge", "repo_id", "number", "source_branch", "branch_hash", "opened_at",
           "merged_at", "closed_at", "state", "review_rounds", "hours_to_first_approval",
           "ci_failures", "assisted_by"]
CR_COMMIT_COLS = ["cr_id", "patch_id", "sha"]
CR_TICKET_COLS = ["cr_id", "ticket_id", "source"]
