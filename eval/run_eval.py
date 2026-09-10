"""Offline skill evaluation runner: headless harness run + OPA grading, recorded in std.eval.* form.

    stdtel-eval --harness claude-code --tasks eval/tasks.yaml --out eval/results.jsonl [--dry-run]

Runners are pluggable; the default shells out to `claude -p` or `copilot` and `opa eval`.
`--dry-run` exercises the pipeline without calling any harness (used by CI smoke test).
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import yaml
from stdtel.manifest import load_catalogue

def run_harness(harness: str, prompt: str, workdir: Path, skill_dir: Path | None, dry_run: bool) -> dict:
    if dry_run:
        return {"total_tokens": 1234 if skill_dir else 1500, "ok": True}
    if harness == "claude-code":
        env = dict(os.environ)
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if skill_dir: cmd += ["--add-dir", str(skill_dir)]
        out = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=900)
        data = json.loads(out.stdout or "{}")
        u = data.get("usage", {})
        return {"total_tokens": sum(int(u.get(k, 0)) for k in ("input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens")), "ok": out.returncode == 0}
    if harness == "copilot-cli":
        out = subprocess.run(["copilot", "-p", prompt, "--allow-all-tools"], cwd=workdir, capture_output=True, text=True, timeout=900)
        return {"total_tokens": 0, "ok": out.returncode == 0}   # token count from OTel export, not stdout
    raise SystemExit(f"unknown harness {harness}")

GRADED_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".go", ".java", ".rb", ".cs",
                   ".yaml", ".yml", ".md"}   # .md so SKILL.md manifests are gradeable
MAX_GRADED_BYTES = 512_000
LOG_CALL = re.compile(r"\b(?:logger|logging|log|LOG)\s*\.\s*"
                      r"(?:debug|info|warn|warning|error|critical|exception)\s*\(")


def extract_log_calls(content: str) -> list[str]:
    """The full argument list of each logging call, brackets matched.

    Real log calls span several lines, so a line-bound regex misses exactly the
    multi-line calls where a leaked field is most likely to hide. Matching
    brackets here keeps the Rego declarative and precise instead of asking RE2
    to approximate a parser.
    """
    calls = []
    for m in LOG_CALL.finditer(content):
        depth, i = 0, m.end() - 1
        while i < len(content):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    calls.append(content[m.start():i + 1])
                    break
            i += 1
        else:
            calls.append(content[m.start():])      # unbalanced: grade what we have
    return calls


def build_input(workdir: Path) -> dict:
    """The document the Rego policies evaluate.

    `opa eval -i` takes a JSON file, never a directory, so the generated tree is
    collected into {"files": [{path, content}]}. This is local grading of code the
    eval itself produced; nothing here is emitted as telemetry (the eval record
    carries booleans only).
    """
    files = []
    for f in sorted(workdir.rglob("*")):
        if f.is_file() and f.suffix in GRADED_SUFFIXES and f.stat().st_size <= MAX_GRADED_BYTES:
            content = f.read_text(encoding="utf-8", errors="replace")
            files.append({"path": str(f.relative_to(workdir)),
                          "content": content,
                          "log_calls": extract_log_calls(content)})
    return {"files": files}


def grade(workdir: Path, policies: list[str], policy_root: Path, dry_run: bool) -> dict[str, bool]:
    """first-time policy pass per policy id. A policy that cannot be evaluated
    fails loudly and counts as a failure — never a silent pass."""
    if dry_run:
        return {p: True for p in policies}
    if not policy_root.is_dir():
        raise SystemExit(f"policy root {policy_root} does not exist; run with --dry-run or add Rego packages")
    results = {}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(build_input(workdir), fh)
        input_path = fh.name
    try:
        for p in policies:
            out = subprocess.run(
                ["opa", "eval", "-d", str(policy_root), "-i", input_path, f"data.{p}.deny", "--format", "json"],
                capture_output=True, text=True)
            if out.returncode != 0:
                print(f"stdtel-eval: opa failed for {p}: {out.stderr.strip()}", file=sys.stderr)
                results[p] = False
                continue
            try:
                expressions = json.loads(out.stdout)["result"][0]["expressions"][0]["value"]
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                print(f"stdtel-eval: cannot read opa output for {p}: {e}", file=sys.stderr)
                results[p] = False
                continue
            results[p] = not expressions
    finally:
        os.unlink(input_path)
    return results

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", default="claude-code"); ap.add_argument("--tasks", default="eval/tasks.yaml")
    ap.add_argument("--skills", default="skills"); ap.add_argument("--policies", default="policies")
    ap.add_argument("--out", default="eval/results.jsonl"); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    cat = load_catalogue(Path(a.skills)); tasks = yaml.safe_load(Path(a.tasks).read_text())
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "unknown"
    n = 0
    with open(a.out, "a") as fh:
        for t in tasks:
            m = cat[t["skill"]]
            for arm in ("without", "with"):
                with tempfile.TemporaryDirectory() as tmp:
                    wd = Path(tmp)
                    fx = Path("eval") / t.get("fixture", "")
                    if fx.is_dir(): shutil.copytree(fx, wd, dirs_exist_ok=True)
                    t0 = time.time()
                    r = run_harness(a.harness, t["prompt"], wd, m.path.parent if arm == "with" else None, a.dry_run)
                    g = grade(wd, t["policies"], Path(a.policies), a.dry_run)
                    rec = {"eval_id": str(uuid.uuid4()), "task_id": t["id"], "arm": arm, "skill_name": m.name, "skill_version": m.version,
                           "harness": a.harness, "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "passed": all(g.values()),
                           "policy_results": g, "total_tokens": r["total_tokens"], "duration_seconds": round(time.time() - t0, 2),
                           "catalogue_commit": commit}
                    fh.write(json.dumps(rec) + "\n"); n += 1
    print(f"wrote {n} eval record(s) to {a.out}"); return 0

if __name__ == "__main__":
    sys.exit(main())
