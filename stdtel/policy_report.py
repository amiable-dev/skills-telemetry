"""Grade a PR's tree with OPA and emit `policy_result` rows.

The primary effectiveness metric is *first-time* OPA policy pass rate, so
`run_seq` — which CI run this was — is the metric, not metadata. It cannot be
recovered afterwards: re-runs, retries and cancelled jobs make the true ordering
unknowable from history, which is why this runs in CI and writes an artefact
rather than being reconstructed by the loader (ADR-002).

    stdtel-policy-report --pr-id owner/repo#42 --run-seq 1 --out policy.jsonl

Grading reuses `eval.run_eval`, so CI and the offline eval runner cannot diverge
on what "passing" means.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

COLUMNS = ("pr_id", "policy_id", "run_seq", "passed", "evaluated_at")


def next_run_seq(previous_runs: int) -> int:
    """1 for the first CI run on a PR, 2 for the next, and so on.

    Derived from a count of prior completed runs rather than a retry counter:
    `run_attempt` counts re-runs of one workflow run, not runs of the workflow.
    """
    if previous_runs < 0:
        raise ValueError(f"previous_runs cannot be negative: {previous_runs}")
    return previous_runs + 1


def count_previous_runs(repo: str, workflow: str, branch: str) -> int:
    """Completed runs of this workflow on this branch, via `gh`.

    Returns 0 when it cannot tell. That biases toward calling a run "first",
    which is the safer error: a duplicated run_seq=1 is visible as a conflict in
    the warehouse, whereas silently skipping to 2 would lose the first-time
    measurement entirely.
    """
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs",
         "-f", f"branch={branch}", "-f", "status=completed", "--jq", ".total_count"],
        capture_output=True, text=True)
    if out.returncode != 0:
        print(f"stdtel-policy-report: cannot count prior runs ({out.stderr.strip()}); "
              f"treating this as the first", file=sys.stderr)
        return 0
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def build_rows(workdir: Path, policies: list[str], policy_root: Path,
               pr_id: str, run_seq: int) -> list[dict]:
    """One row per policy. A policy that cannot be evaluated fails; it is never
    omitted, because an absent row and a failing row must not look alike."""
    from eval.run_eval import grade

    evaluated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    results = grade(Path(workdir), policies, Path(policy_root), dry_run=False)
    return [{"pr_id": pr_id, "policy_id": policy_id, "run_seq": run_seq,
             "passed": bool(passed), "evaluated_at": evaluated_at}
            for policy_id, passed in results.items()]


def write_jsonl(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps({c: row[c] for c in COLUMNS}) + "\n")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stdtel-policy-report", description=__doc__.splitlines()[0])
    ap.add_argument("--pr-id", required=True, help="owner/repo#number, matching pull_request.pr_id")
    ap.add_argument("--policies", type=Path, default=Path("policies"))
    ap.add_argument("--policy-ids", help="comma-separated; defaults to every package under --policies")
    ap.add_argument("--workdir", type=Path, default=Path("."))
    ap.add_argument("--out", type=Path, default=Path("policy-results.jsonl"))
    run = ap.add_mutually_exclusive_group(required=True)
    run.add_argument("--run-seq", type=int, help="explicit sequence number")
    run.add_argument("--derive-run-seq", nargs=3, metavar=("REPO", "WORKFLOW", "BRANCH"),
                     help="count prior completed runs with gh and use the next number")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if not a.policies.is_dir():
        print(f"stdtel-policy-report: no policy root at {a.policies}", file=sys.stderr)
        return 2

    if a.policy_ids:
        policy_ids = [p.strip() for p in a.policy_ids.split(",") if p.strip()]
    else:
        policy_ids = discover_policy_ids(a.policies)
    if not policy_ids:
        print(f"stdtel-policy-report: no policies found under {a.policies}", file=sys.stderr)
        return 2

    run_seq = a.run_seq if a.run_seq is not None else next_run_seq(count_previous_runs(*a.derive_run_seq))
    rows = build_rows(a.workdir, policy_ids, a.policies, a.pr_id, run_seq)
    write_jsonl(a.out, rows)
    failed = [r["policy_id"] for r in rows if not r["passed"]]
    print(f"wrote {len(rows)} policy result(s) to {a.out} (run_seq={run_seq}); "
          f"{'failed: ' + ', '.join(failed) if failed else 'all passed'}")
    return 0


def discover_policy_ids(policy_root: Path) -> list[str]:
    """Rego `package` declarations under the root, which are the policy ids."""
    import re
    ids = set()
    for rego in sorted(Path(policy_root).rglob("*.rego")):
        m = re.search(r"^package\s+([\w.]+)", rego.read_text(), re.M)
        if m:
            ids.add(m.group(1))
    return sorted(ids)


if __name__ == "__main__":
    raise SystemExit(main())
