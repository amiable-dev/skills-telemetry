"""Parse and validate SKILL.md front-matter (the standards-repo contract)."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
STANDARD_ID = re.compile(r"^STD-[A-Z]+-\d{3}$")
HARNESSES = {"claude-code", "copilot-vscode", "copilot-cli"}
SUCCESS_SIGNALS = {"policy", "test", "manual"}


@dataclass
class SkillManifest:
    name: str
    version: str
    standard_id: str
    policy_ids: list[str]
    owner: str
    harness_support: list[str]
    telemetry_emit: bool = True
    success_signal: str = "policy"
    path: Path | None = None
    extra: dict = field(default_factory=dict)

    def as_attributes(self) -> dict:
        """Span attributes contributed by the manifest (std.* namespace)."""
        return {
            "std.skill.name": self.name,
            "std.skill.version": self.version,
            "std.standard_id": self.standard_id,
            "std.policy.ids": ",".join(self.policy_ids),
            "std.skill.owner": self.owner,
        }


class ManifestError(ValueError):
    pass


def split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ManifestError("SKILL.md must start with YAML front-matter (---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ManifestError("unterminated front-matter")
    data = yaml.safe_load(parts[1]) or {}
    return data, parts[2]


def parse_manifest(text: str, path: Path | None = None) -> SkillManifest:
    data, _ = split_front_matter(text)
    errors = []
    for key in ("name", "version", "standard_id", "policy_ids", "owner", "harness_support"):
        if key not in data:
            errors.append(f"missing required field: {key}")
    if errors:
        raise ManifestError("; ".join(errors))
    if not SEMVER.match(str(data["version"])):
        errors.append(f"version must be semver, got {data['version']!r}")
    if not STANDARD_ID.match(data["standard_id"]):
        errors.append(f"standard_id must match STD-XXX-000, got {data['standard_id']!r}")
    if not isinstance(data["policy_ids"], list) or not data["policy_ids"]:
        errors.append("policy_ids must be a non-empty list")
    bad = set(data["harness_support"]) - HARNESSES
    if bad:
        errors.append(f"unknown harness(es): {sorted(bad)}")
    tel = data.get("telemetry", {}) or {}
    signal = tel.get("success_signal", "policy")
    if signal not in SUCCESS_SIGNALS:
        errors.append(f"telemetry.success_signal must be one of {sorted(SUCCESS_SIGNALS)}")
    if errors:
        raise ManifestError("; ".join(errors))
    known = {"name", "version", "standard_id", "policy_ids", "owner", "harness_support", "telemetry"}
    return SkillManifest(
        name=data["name"],
        version=str(data["version"]),
        standard_id=data["standard_id"],
        policy_ids=list(data["policy_ids"]),
        owner=data["owner"],
        harness_support=list(data["harness_support"]),
        telemetry_emit=bool(tel.get("emit", True)),
        success_signal=signal,
        path=path,
        extra={k: v for k, v in data.items() if k not in known},
    )


def load_manifest(path: Path) -> SkillManifest:
    return parse_manifest(path.read_text(encoding="utf-8"), path=path)


def load_catalogue(root: Path) -> dict[str, SkillManifest]:
    """All SKILL.md files under root, keyed by skill name."""
    out: dict[str, SkillManifest] = {}
    for p in sorted(root.rglob("SKILL.md")):
        m = load_manifest(p)
        if m.name in out:
            raise ManifestError(f"duplicate skill name {m.name!r}: {p} and {out[m.name].path}")
        out[m.name] = m
    return out


def cli(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0] if argv else "skills")
    try:
        cat = load_catalogue(root)
    except ManifestError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1
    for name, m in cat.items():
        print(f"OK  {name}@{m.version}  {m.standard_id}  policies={len(m.policy_ids)}")
    print(f"{len(cat)} skill(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
