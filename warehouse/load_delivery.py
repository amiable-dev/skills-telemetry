"""Load delivery data from GitHub into ticket / pull_request / policy_result / defect.

    python -m warehouse.load_delivery --repo owner/name --dsn postgresql://... --since 30d
    python -m warehouse.load_delivery --repo owner/name --dry-run          # print rows, touch nothing

This is the other half of the primary metric. `skill_invocation` says what a skill
cost; these tables say what happened to the work it was used on, joined on
`std.ticket.id` — the ticket key parsed from the branch, which is why
ticket-prefixed branches are a hard requirement rather than a convention.

Requires the `gh` CLI, already authenticated. Policy results come from a CI
artefact rather than the API: `run_seq` (which CI run this was) cannot be
recovered after the fact, and "first-time pass" is the whole metric.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

from stdtel.enrich import ticket_from_branch

PR_FIELDS = ("number,title,headRefName,createdAt,mergedAt,closedAt,labels,"
             "reviews,reviewDecision,url,author,additions,deletions,changedFiles")
HARNESS_LABELS = {"claude-code": "claude-code", "copilot": "copilot", "no-ai": "none"}


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


def assisted_by(labels: list[dict]) -> str:
    """Which harness produced this PR, from its labels.

    The crossover design tags each ticket with its arm; the label is how that
    reaches the warehouse. Unlabelled PRs are 'unknown', never silently 'none' —
    an unlabelled PR is missing data, not a no-AI control.
    """
    names = {l.get("name", "").lower() for l in labels or []}
    for label, arm in HARNESS_LABELS.items():
        if label in names:
            return arm
    return "unknown"


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


def parse_pr(pr: dict, repo: str) -> dict:
    opened, merged = _ts(pr.get("createdAt")), _ts(pr.get("mergedAt"))
    return {
        "pr_id": f"{repo}#{pr['number']}",
        "repo": repo,
        "ticket_id": ticket_from_branch(pr.get("headRefName") or ""),
        "opened_at": opened,
        "merged_at": merged,
        "review_rounds": review_rounds(pr.get("reviews")),
        "hours_to_first_approval": _hours(opened, first_approval_at(pr.get("reviews"))),
        "ci_failures": None,          # populated from the CI artefact, not the API
        "assisted_by": assisted_by(pr.get("labels")),
    }


def ticket_rows(prs: list[dict], team: str) -> list[dict]:
    """One ticket row per ticket key seen, with cycle time derived from its PRs.

    Real cycle time starts when work starts, which lives in Linear. Until that
    source is wired, first-PR-opened to last-PR-merged is the honest proxy — it
    understates cycle time and is labelled as a proxy wherever it surfaces.
    """
    by_ticket: dict[str, list[dict]] = {}
    for pr in prs:
        if pr["ticket_id"] != "unattributed":
            by_ticket.setdefault(pr["ticket_id"], []).append(pr)
    rows = []
    for ticket_id, group in by_ticket.items():
        opened = min((p["opened_at"] for p in group if p["opened_at"]), default=None)
        merged = [p["merged_at"] for p in group if p["merged_at"]]
        done = max(merged) if merged else None
        arms = {p["assisted_by"] for p in group} - {"unknown"}
        rows.append({
            "ticket_id": ticket_id, "team": team, "story_points": None,
            "created_at": opened, "started_at": opened, "done_at": done,
            "cycle_time_hours": _hours(opened, done), "lead_time_hours": _hours(opened, done),
            "harness_arm": arms.pop() if len(arms) == 1 else "mixed" if arms else "unknown",
        })
    return rows


def policy_rows(path: Path) -> list[dict]:
    """Policy results from a CI artefact: JSONL of
    {pr_id, policy_id, run_seq, passed, evaluated_at}."""
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            r["evaluated_at"] = _ts(r.get("evaluated_at"))
            rows.append(r)
    return rows


def defect_rows(issues: list[dict], repo: str) -> list[dict]:
    rows = []
    for issue in issues:
        body = f"{issue.get('title','')} {issue.get('body','')}"
        m = re.search(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b", body.upper())
        rows.append({
            "defect_id": f"{repo}#{issue['number']}",
            "ticket_id": m.group(1) if m else None,
            "opened_at": _ts(issue.get("createdAt")),
            "severity": next((l["name"] for l in issue.get("labels", [])
                              if l.get("name", "").lower().startswith(("sev", "p0", "p1"))), None),
            "source": "github",
        })
    return rows


TABLES = {
    "pull_request": ["pr_id", "repo", "ticket_id", "opened_at", "merged_at", "review_rounds",
                     "hours_to_first_approval", "ci_failures", "assisted_by"],
    "ticket": ["ticket_id", "team", "story_points", "created_at", "started_at", "done_at",
               "cycle_time_hours", "lead_time_hours", "harness_arm"],
    "policy_result": ["pr_id", "policy_id", "run_seq", "passed", "evaluated_at"],
    "defect": ["defect_id", "ticket_id", "opened_at", "severity", "source"],
}
CONFLICT_KEYS = {"pull_request": "pr_id", "ticket": "ticket_id",
                 "policy_result": "pr_id, policy_id, run_seq", "defect": "defect_id"}


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
    a = ap.parse_args(argv)
    if not a.dry_run and not a.dsn:
        raise SystemExit("--dsn is required unless --dry-run")

    days = int(a.since.rstrip("d"))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()

    prs = [parse_pr(p, a.repo) for p in gh_json(
        ["pr", "list", "--repo", a.repo, "--state", "merged", "--limit", str(a.limit),
         "--search", f"merged:>={cutoff}", "--json", PR_FIELDS])]
    issues = gh_json(["issue", "list", "--repo", a.repo, "--state", "all", "--label", "bug",
                      "--limit", str(a.limit), "--json", "number,title,body,createdAt,labels"])
    batches = {
        "pull_request": prs,
        "ticket": ticket_rows(prs, a.team),
        "defect": defect_rows(issues, a.repo),
        "policy_result": policy_rows(a.policy_results) if a.policy_results else [],
    }
    for table, rows in batches.items():
        if a.dry_run:
            print(f"{table}: {len(rows)} row(s)")
            for r in rows[:3]:
                print("  ", json.dumps(r, default=str))
        else:
            print(f"{table}: loaded {write(a.dsn, table, rows)} row(s)")
    if not batches["policy_result"]:
        print("policy_result: EMPTY — first-time pass rate cannot be computed without it "
              "(pass --policy-results from CI)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
