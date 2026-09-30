"""The GitHub adapter for the change-request record (ADR-013).

    python -m warehouse.load_delivery --repo owner/name --dsn postgresql://... --since 30d
    python -m warehouse.load_delivery --repo owner/name --dry-run          # print rows, touch nothing

This is the other half of the primary metric. Capture says what a skill cost;
these tables say what happened to the work it was used on. The join is not a
ticket parsed from a branch name — that needed a naming convention nobody was
told about and invented fake tickets — but a branch identity plus a time window,
confirmed by commit patch-ids. The neutral record and its helpers live in
`change_requests.py`; everything GitHub-shaped lives here, so a GitLab adapter
(#103) is a sibling of this file, not a change to the schema.

Requires the `gh` CLI, already authenticated. Policy results come from a CI
artefact rather than the API: `run_seq` (which CI run this was) cannot be
recovered after the fact, and "first-time pass" is the whole metric.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import uuid
from pathlib import Path

from warehouse.change_requests import (CR_COLS, CR_COMMIT_COLS, CR_TICKET_COLS, branch_hash, cr_id,
                                       key_in, repo_id, ticket_links, ticket_rows)
from warehouse.load_traces import record_run

FORGE = "github"

PR_FIELDS = ("number,title,body,state,headRefName,headRepository,headRepositoryOwner,"
             "isCrossRepository,closingIssuesReferences,createdAt,mergedAt,closedAt,labels,"
             "reviews,reviewDecision,url,author")
HARNESS_LABELS = {"claude-code": "claude-code", "copilot": "copilot", "no-ai": "none"}

#: Coding agents, by every spelling gh has been seen to use (ADR-014 decision 9,
#: verified 2026-09-30 on real PRs — tests/test_agent_prs.py names them). `gh pr
#: list` gives `app/<slug>`; `gh search` gives the display login with type Bot.
AGENT_LOGINS = {
    "app/copilot-swe-agent": "copilot", "copilot-swe-agent[bot]": "copilot",
    "app/claude": "claude-code", "claude[bot]": "claude-code",
}
#: Display logins that are an agent only when the forge says the author is a bot.
AGENT_DISPLAY_LOGINS = {"copilot": "copilot"}
ASSISTED_LABEL = "claude-code-assisted"        # Anthropic's contribution-metrics label


def agent_harness(pr: dict) -> str | None:
    """The harness whose coding agent opened this PR, or None."""
    author = pr.get("author") or {}
    login = str(author.get("login") or "")
    if login.lower() in AGENT_LOGINS:
        return AGENT_LOGINS[login.lower()]
    if author.get("type") == "Bot" or author.get("is_bot"):
        return AGENT_DISPLAY_LOGINS.get(login.lower())
    return None


def is_bot(pr: dict) -> bool:
    """A PR opened by a bot that is not a coding agent.

    Dependabot and its kind are not developer work: counting them inflates the
    "without the skill" cohort with PRs no developer wrote, no skill could have
    helped, and whose branch names are the phantom-ticket source above. gh's
    GraphQL author carries `is_bot`; the REST spelling is a `[bot]` suffix and
    GitHub Apps appear as `app/<name>`. All three are checked, because which one
    arrives depends on the gh version rather than on anything we control.
    """
    if agent_harness(pr):
        return False            # agent work for its harness (ADR-014 decision 9), not a bot
    author = pr.get("author") or {}
    login = str(author.get("login") or "")
    return bool(author.get("is_bot")) or login.endswith("[bot]") or login.startswith("app/")



def gh_json(args: list[str]) -> list | dict:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"gh failed: {' '.join(args)}\n{out.stderr.strip()}")
    return json.loads(out.stdout or "[]")


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hours(a: dt.datetime | None, b: dt.datetime | None) -> float | None:
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 3)


def assisted_by(labels: list[dict], author: dict | None = None) -> str:
    """Which harness produced this PR.

    Every signal naming a harness is evidence: the crossover design's arm label,
    Anthropic's `claude-code-assisted` label, and a coding agent as author
    (ADR-014 decision 9). One harness evidenced is that harness — even under a
    `no-ai` label, because a control an agent touched is not a control. Two is
    `mixed`, as the ticket rollup already spells it. `none` needs the `no-ai`
    label and nothing against it. Otherwise 'unknown', never silently 'none':
    an unlabelled PR is missing data, and the Anthropic label exists only on some
    plans, so its absence says nothing.
    """
    names = {l.get("name", "").lower() for l in labels or []}
    evidence = {arm for label, arm in HARNESS_LABELS.items() if label in names and arm != "none"}
    if ASSISTED_LABEL in names:
        evidence.add("claude-code")
    agent = agent_harness({"author": author})
    if agent:
        evidence.add(agent)
    if len(evidence) == 1:
        return evidence.pop()
    if evidence:
        return "mixed"
    return "none" if "no-ai" in names else "unknown"


def review_rounds(reviews: list[dict]) -> int:
    """Rounds of review, not review comments.

    A round is a CHANGES_REQUESTED that was followed by more review activity;
    counting raw reviews would make a single thorough reviewer look like churn.
    """
    states = [r.get("state") for r in reviews or []]
    return sum(1 for s in states if s == "CHANGES_REQUESTED")


def first_approval_at(reviews: list[dict]) -> dt.datetime | None:
    for r in sorted(reviews or [], key=lambda r: r.get("submittedAt") or ""):
        if r.get("state") == "APPROVED":
            return _ts(r.get("submittedAt"))
    return None


def _github(owner_name: str) -> str:
    return repo_id(f"https://github.com/{owner_name}")


def to_change_request(pr: dict, base: str) -> dict:
    """One GitHub PR as a neutral change-request row.

    `base` is the repository the PR targets and owns it. The branch hash uses the
    repository the *branch* lives in, which for a fork is the contributor's:
    their hook sees the fork's remote, so hashing the base would never join.
    """
    opened, merged, closed = _ts(pr.get("createdAt")), _ts(pr.get("mergedAt")), _ts(pr.get("closedAt"))
    head_owner = (pr.get("headRepositoryOwner") or {}).get("login") or base.split("/")[0]
    head_name = (pr.get("headRepository") or {}).get("name") or base.split("/")[-1]
    head_repo = _github(f"{head_owner}/{head_name}") if pr.get("isCrossRepository") else _github(base)
    state = "merged" if merged else "closed" if closed else "open"
    return {
        "cr_id": cr_id(FORGE, _github(base), pr["number"]),
        "forge": FORGE,
        "repo_id": _github(base),
        "number": pr["number"],
        "source_branch": pr.get("headRefName") or "",
        "branch_hash": branch_hash(head_repo, pr.get("headRefName") or ""),
        "opened_at": opened, "merged_at": merged, "closed_at": closed, "state": state,
        "review_rounds": review_rounds(pr.get("reviews")),
        "hours_to_first_approval": _hours(opened, first_approval_at(pr.get("reviews"))),
        "ci_failures": None,          # populated from the CI artefact, not the API
        "assisted_by": assisted_by(pr.get("labels"), pr.get("author")),
    }


def ticket_link_rows(pr: dict, base: str) -> list[dict]:
    closes = [i.get("number") for i in pr.get("closingIssuesReferences") or [] if i.get("number")]
    this = cr_id(FORGE, _github(base), pr["number"])
    return [{"cr_id": this, "ticket_id": ticket, "source": source}
            for ticket, source in ticket_links(FORGE, _github(base), closes, pr.get("title") or "",
                                               pr.get("body") or "", pr.get("headRefName") or "")]


def _patch_id(patch: str) -> str | None:
    """`git patch-id` works with no repository, reading the patch on stdin; the
    GitHub patch of a commit gives the same id as local git (verified on
    6aa924d, 2026-09-30), so no clone is needed."""
    if not patch.strip():
        return None
    try:
        out = subprocess.run(["git", "patch-id", "--stable"], input=patch, capture_output=True,
                             text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out[0] if out else None


def commit_rows(this_cr: str, shas: list[str], fetch_patch) -> list[dict]:
    """(cr_id, patch_id, sha) per commit. A commit whose patch cannot be read is
    skipped, never given an invented id."""
    rows = []
    for sha in shas:
        pid = _patch_id(fetch_patch(sha) or "")
        if pid:
            rows.append({"cr_id": this_cr, "patch_id": pid, "sha": sha})
    return rows


def _pr_commits(base: str, number: int) -> list[str]:
    data = gh_json(["api", "--paginate", f"repos/{base}/pulls/{number}/commits"])
    return [c["sha"] for c in data if c.get("sha")]


def _commit_patch(base: str, sha: str) -> str:
    out = subprocess.run(["gh", "api", f"repos/{base}/commits/{sha}",
                          "-H", "Accept: application/vnd.github.patch"],
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def _cr_from_ci(pr_id: str) -> str:
    """CI writes `owner/repo#number`, GitHub-shaped; the adapter owns the mapping."""
    owner_name, _, number = (pr_id or "").partition("#")
    return cr_id(FORGE, _github(owner_name), number) if number else ""


def policy_rows(path: Path) -> list[dict]:
    """Policy results from a CI artefact: JSONL of
    {pr_id, policy_id, run_seq, passed, evaluated_at}, keyed onto change requests."""
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            rows.append({"cr_id": _cr_from_ci(r.get("pr_id", "")), "policy_id": r.get("policy_id"),
                         "run_seq": r.get("run_seq"), "passed": r.get("passed"),
                         "evaluated_at": _ts(r.get("evaluated_at"))})
    return rows


def defect_rows(issues: list[dict], repo: str) -> list[dict]:
    rows = []
    for issue in issues:
        rows.append({
            "defect_id": f"{repo}#{issue['number']}",
            "ticket_id": key_in(issue.get("title", "")) or key_in(issue.get("body", "")),
            "opened_at": _ts(issue.get("createdAt")),
            "severity": next((l["name"] for l in issue.get("labels", [])
                              if l.get("name", "").lower().startswith(("sev", "p0", "p1"))), None),
            "source": "github",
        })
    return rows


TABLES = {
    "change_request": CR_COLS,
    "change_request_commit": CR_COMMIT_COLS,
    "change_request_ticket": CR_TICKET_COLS,
    "ticket": ["ticket_id", "team", "story_points", "created_at", "started_at", "done_at",
               "cycle_time_hours", "lead_time_hours", "harness_arm"],
    "policy_result": ["cr_id", "policy_id", "run_seq", "passed", "evaluated_at"],
    "defect": ["defect_id", "ticket_id", "opened_at", "severity", "source"],
}
CONFLICT_KEYS = {"change_request": "cr_id", "change_request_commit": "cr_id, patch_id",
                 "change_request_ticket": "cr_id, ticket_id", "ticket": "ticket_id",
                 "policy_result": "cr_id, policy_id, run_seq", "defect": "defect_id"}


def write(dsn: str, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    import psycopg
    cols = TABLES[table]
    sql = (f"INSERT INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('%(' + c + ')s' for c in cols)}) "
           f"ON CONFLICT ({CONFLICT_KEYS[table]}) DO NOTHING")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(sql, [{c: r.get(c) for c in cols} for r in rows])
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--dsn")
    ap.add_argument("--since", default="30d")
    ap.add_argument("--team", default="unknown")
    ap.add_argument("--policy-results", type=Path, help="JSONL artefact from CI")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commits", action="store_true",
                    help="skip commit patch-ids (one API call per commit); the branch join still works")
    a = ap.parse_args(argv)
    if not a.dry_run and not a.dsn:
        raise SystemExit("--dsn is required unless --dry-run")

    days = int(a.since.rstrip("d"))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()
    # ADR-009 decision 7: every run writes its own row, failures included. A
    # loader that records only its successes cannot be told from one that was
    # never run, which is how the warehouse came to sit two days behind Tempo.
    run = {"run_id": uuid.uuid4().hex, "loader": "load_delivery",
           "started_at": dt.datetime.now(dt.timezone.utc), "finished_at": None,
           "rows_loaded": 0, "source_max_ts": None, "ok": False, "error": None}
    try:
        # every state (ADR-013 decision 3): closed-unmerged work has a cost, and
        # an open PR is where in-flight work joins until it closes
        raw = gh_json(["pr", "list", "--repo", a.repo, "--state", "all", "--limit", str(a.limit),
                       "--search", f"updated:>={cutoff}", "--json", PR_FIELDS])
        bots = [p for p in raw if is_bot(p)]
        if bots:
            print(f"skipped {len(bots)} bot-authored PR(s) (#62)", file=sys.stderr)
        humans = [p for p in raw if not is_bot(p)]
        prs = [to_change_request(p, a.repo) for p in humans]
        links = [row for p in humans for row in ticket_link_rows(p, a.repo)]
        commits = [] if a.no_commits else [
            row for p, c in zip(humans, prs)
            for row in commit_rows(c["cr_id"], _pr_commits(a.repo, p["number"]),
                                   lambda sha: _commit_patch(a.repo, sha))]
        issues = gh_json(["issue", "list", "--repo", a.repo, "--state", "all", "--label", "bug",
                          "--limit", str(a.limit), "--json", "number,title,body,createdAt,labels"])
        batches = {
            "change_request": prs,
            "change_request_commit": commits,
            "change_request_ticket": links,
            "ticket": ticket_rows(prs, links, a.team),
            "defect": defect_rows(issues, a.repo),
            "policy_result": policy_rows(a.policy_results) if a.policy_results else [],
        }
        run["source_max_ts"] = max((p["closed_at"] or p["opened_at"] for p in prs), default=None)
        for table, rows in batches.items():
            if a.dry_run:
                print(f"{table}: {len(rows)} row(s)")
                for r in rows[:3]:
                    print("  ", json.dumps(r, default=str))
            else:
                run["rows_loaded"] += write(a.dsn, table, rows)
                print(f"{table}: loaded {len(rows)} row(s)")
        run["ok"] = True
    except (Exception, SystemExit) as e:        # noqa: BLE001 - every run writes a row
        run["error"] = f"{type(e).__name__}: {e}"[:2000]
        run["finished_at"] = dt.datetime.now(dt.timezone.utc)
        if not a.dry_run:
            record_run(a.dsn, run)
        raise
    run["finished_at"] = dt.datetime.now(dt.timezone.utc)
    if not a.dry_run:
        record_run(a.dsn, run)
    if not batches["policy_result"]:
        print("policy_result: EMPTY — first-time pass rate cannot be computed without it "
              "(pass --policy-results from CI)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
