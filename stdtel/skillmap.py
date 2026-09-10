"""Generate the Copilot skill lookup table from the skills catalogue."""
from __future__ import annotations
import sys
from pathlib import Path
import yaml
from stdtel.manifest import load_catalogue

def generate(root: Path) -> str:
    cat = load_catalogue(root)
    table = {n: {"standard_id": m.standard_id, "version": m.version, "policy_ids": ",".join(m.policy_ids)}
             for n, m in cat.items()}
    return yaml.safe_dump(table, sort_keys=True)

if __name__ == "__main__":
    print(generate(Path(sys.argv[1] if len(sys.argv) > 1 else "skills")), end="")
