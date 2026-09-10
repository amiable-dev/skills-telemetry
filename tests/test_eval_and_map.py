import json
from pathlib import Path
from eval.run_eval import main as eval_main
from stdtel.skillmap import generate

def test_eval_dry_run(tmp_path):
    out = tmp_path / "r.jsonl"
    assert eval_main(["--dry-run", "--out", str(out)]) == 0
    recs = [json.loads(l) for l in out.read_text().splitlines()]
    assert {r["arm"] for r in recs} == {"with", "without"}
    assert all(r["passed"] for r in recs)

def test_skill_map_matches_committed():
    assert generate(Path("skills")) == Path("collector/copilot-skill-map.yaml").read_text().split("\n", 2)[2]
