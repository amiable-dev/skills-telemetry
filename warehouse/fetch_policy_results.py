"""Collect the `policy-results` artefact from CI runs into one JSONL file.

The primary effectiveness metric is first-time OPA policy pass rate on the PR.
`ci.yml` has produced a `policy-results` artefact on every pull-request run since
the job was written, and **nothing has ever collected it** — `load_delivery.py`
takes `--policy-results <file>` and no automation passed one. So the metric the
whole project exists to compute has had no input, while every CI run went green
(#21).

This is the missing step. It is deliberately a separate module rather than a
flag buried in the delivery loader: fetching is network work against GitHub that
fails in its own ways (an artefact expires after 90 days, a run may predate the
job, a fork PR may not upload one), and each of those has to be *reported*
rather than silently yielding fewer rows.

    python -m warehouse.fetch_policy_results --repo owner/name --out results.jsonl
    python -m warehouse.load_delivery --repo owner/name --policy-results results.jsonl

`run_seq` is not recomputed here. It is stamped in CI at the moment of the run,
because after the fact re-runs and retries make the true ordering unrecoverable
and "first-time pass" is the entire metric (ADR-002).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ARTEFACT_NAME = "policy-results"
DEFAULT_WORKFLOW = "ci.yml"

#: A row is identified by these three. The same (pr_id, policy_id, run_seq) can
#: arrive twice — a re-run of the same CI run re-uploads the artefact — and
#: loading it twice is harmless but double-counts in any group-by done before
#: the insert, so it is collapsed here.
KEY = ("pr_id", "policy_id", "run_seq")


class FetchError(RuntimeError):
    pass


def _gh(args: list[str], check: bool = True) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise FetchError(f"gh {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    return out.stdout


def pr_runs(repo: str, workflow: str = DEFAULT_WORKFLOW, limit: int = 100) -> list[dict]:
    """Recent pull-request runs of the workflow, newest first.

    Only `pull_request` runs carry the artefact: the job is guarded by
    `if: github.event_name == 'pull_request'`, so a push run produces nothing and
    counting it as a miss would make the report look worse than reality.
    """
    raw = _gh(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit),
               "--json", "databaseId,event,conclusion,headBranch,createdAt"])
    runs = json.loads(raw or "[]")
    return [r for r in runs if r.get("event") == "pull_request"]


def artefact_state(repo: str, run_id: int) -> str:
    """`present`, `expired`, or `absent` for this run's policy-results artefact."""
    raw = _gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
               "--jq", ".artifacts[] | {name, expired} | tostring"], check=False)
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("name") == ARTEFACT_NAME:
            return "expired" if item.get("expired") else "present"
    return "absent"


def download(repo: str, run_id: int, into: Path) -> list[dict]:
    """The rows in one run's artefact. Empty when it cannot be read."""
    into.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["gh", "run", "download", str(run_id), "--repo", repo,
                          "--name", ARTEFACT_NAME, "--dir", str(into)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return []
    rows = []
    for path in into.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"stdtel: unparseable line in {ARTEFACT_NAME} for run {run_id}",
                          file=sys.stderr)
    return rows


def collect(repo: str, workflow: str = DEFAULT_WORKFLOW, limit: int = 100,
            workdir: Path | None = None) -> tuple[list[dict], dict]:
    """Every policy result reachable from recent CI runs, with a report.

    The report is not decoration. A run whose artefact has expired is data that
    is gone for good, and one that never produced an artefact is a hole in the
    metric for that PR — both change how much the resulting pass rate is worth,
    and neither is visible in the row count alone (ADR-005).
    """
    runs = pr_runs(repo, workflow, limit)
    report = {"runs_inspected": len(runs), "artefact_present": 0, "artefact_expired": 0,
              "artefact_absent": 0, "download_failed": 0, "rows": 0, "duplicates_collapsed": 0}
    seen: dict[tuple, dict] = {}
    base = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="stdtel-policy-"))
    for run in runs:
        run_id = run["databaseId"]
        state = artefact_state(repo, run_id)
        report[f"artefact_{state}"] += 1
        if state != "present":
            continue
        rows = download(repo, run_id, base / str(run_id))
        if not rows:
            report["download_failed"] += 1
            continue
        for row in rows:
            key = tuple(row.get(k) for k in KEY)
            if key in seen:
                report["duplicates_collapsed"] += 1
                continue
            seen[key] = row
    report["rows"] = len(seen)
    return list(seen.values()), report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stdtel-fetch-policy-results", description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    ap.add_argument("--limit", type=int, default=100, help="how many recent runs to inspect")
    ap.add_argument("--out", type=Path, default=Path("policy-results.jsonl"))
    a = ap.parse_args(argv)

    try:
        rows, report = collect(a.repo, a.workflow, a.limit)
    except FetchError as e:
        print(f"stdtel: {e}", file=sys.stderr)
        return 1

    a.out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{report['rows']} policy result(s) from {report['artefact_present']} artefact(s) "
          f"across {report['runs_inspected']} pull-request run(s) -> {a.out}")
    if report["artefact_expired"]:
        print(f"stdtel: {report['artefact_expired']} run(s) had an EXPIRED artefact. Those PRs "
              f"carry no first-time pass result and never will — GitHub deletes artefacts after "
              f"90 days. Load more often, or raise the retention on the upload step.",
              file=sys.stderr)
    if report["artefact_absent"]:
        print(f"stdtel: {report['artefact_absent']} pull-request run(s) produced no artefact at "
              f"all (the job predates it, was skipped, or the run failed before it). Those PRs "
              f"are a hole in the metric rather than a failing result.", file=sys.stderr)
    if report["download_failed"]:
        print(f"stdtel: {report['download_failed']} artefact(s) could not be downloaded.",
              file=sys.stderr)
    if not rows:
        print("stdtel: no policy results collected — first-time pass rate cannot be computed. "
              "Check that ci.yml still uploads the 'policy-results' artefact on pull_request runs.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
