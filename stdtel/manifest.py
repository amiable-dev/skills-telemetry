"""Parse and validate SKILL.md front-matter (the standards-repo contract)."""
from __future__ import annotations

import hashlib
import os
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
    content_hash: str = ""
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
            # The version is asserted by whoever wrote the front-matter; this is
            # observed. Same version, different hash means the guidance changed
            # without a bump, and every comparison drawn from that skill is
            # aggregating two populations (ADR-005: never record an unobserved
            # value). Deliberately NOT a spanmetrics dimension — a new series per
            # edit is #42 again.
            "std.skill.content_hash": self.content_hash,
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


# Agent Skills permits only these six frontmatter keys; everything else is a
# client-only extension that hard-errors on claude.ai upload. Our contract fields
# therefore live under `metadata:`, which the spec types as a string->string map,
# so lists arrive comma-separated.
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
CONTRACT_KEYS = ("version", "standard_id", "policy_ids", "owner", "harness_support", "telemetry")


def _split_list(value) -> list[str]:
    """`policy_ids` as a real list (top-level form) or a comma-separated string
    (spec form, where metadata values must be strings)."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _flatten(data: dict) -> dict:
    """Contract fields, wherever they live.

    Spec-conformant SKILL.md nests them under `metadata:`; the original flat form
    is still accepted so existing catalogues keep validating. `metadata:` wins.
    """
    meta = data.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    out = dict(data)
    for key in CONTRACT_KEYS:
        if key in meta:
            out[key] = meta[key]
    if "telemetry" not in out:
        # spec form flattens the nested telemetry block into dotted metadata keys
        tel = {k.split(".", 1)[1]: v for k, v in meta.items() if k.startswith("telemetry.")}
        if tel:
            out["telemetry"] = tel
    for key in ("policy_ids", "harness_support"):
        if key in out:
            out[key] = _split_list(out[key])
    return out


def _fail(errors: list[str], path: Path | None) -> None:
    """Raise with the file named.

    The path was always available here and was thrown away, so `stdtel-validate`
    on a directory of skills said a manifest was invalid without saying which
    (#49) — leaving the reader to bisect by hand.
    """
    where = f"{path}: " if path is not None else ""
    raise ManifestError(where + "; ".join(errors))


def body_hash(text: str) -> str:
    """Fingerprint of the instruction, not of the file.

    The body only: onboarding a skill edits its `metadata:` block, and that must
    not read as a change to what the skill tells the model.
    """
    _, body = split_front_matter(text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def parse_manifest(text: str, path: Path | None = None) -> SkillManifest:
    data, body = split_front_matter(text)
    data = _flatten(data)
    errors = []
    for key in ("name", "version", "standard_id", "policy_ids", "owner", "harness_support"):
        if key not in data:
            errors.append(f"missing required field: {key}")
    if errors:
        if "metadata" not in data:
            # The single most common shape of a first attempt: contract fields at
            # the top level, or absent entirely. Saying which field is missing
            # does not tell a newcomer that these live under `metadata:`.
            errors.append("contract fields live under `metadata:` — see skills/stdtel-onboard")
        _fail(errors, path)
    tel = data.get("telemetry", {}) or {}
    if not isinstance(tel, dict):
        tel = {}
    signal = str(tel.get("success_signal", "policy"))
    if not SEMVER.match(str(data["version"])):
        errors.append(f"version must be semver, got {data['version']!r}")
    if not STANDARD_ID.match(data["standard_id"]):
        errors.append(f"standard_id must match STD-XXX-000, got {data['standard_id']!r}")
    if not isinstance(data["policy_ids"], list):
        errors.append("policy_ids must be a list")
    elif not data["policy_ids"] and signal == "policy":
        # Empty is honest for a skill nothing verifies; claiming a policy signal
        # and naming no policy is not. Requiring one unconditionally pushed people
        # to cite an unrelated policy to pass the gate, which `stdtel-onboard`
        # forbids and which makes the primary metric score a skill against a rule
        # it has nothing to do with.
        errors.append("policy_ids is empty, so telemetry.success_signal cannot be `policy` — "
                      "name the policies, or set success_signal to test/manual")
    bad = set(data["harness_support"]) - HARNESSES
    if bad:
        errors.append(f"unknown harness(es): {sorted(bad)}")
    if signal not in SUCCESS_SIGNALS:
        errors.append(f"telemetry.success_signal must be one of {sorted(SUCCESS_SIGNALS)}")
    if errors:
        _fail(errors, path)
    known = SPEC_KEYS | set(CONTRACT_KEYS) | {"description"}
    return SkillManifest(
        name=data["name"],
        version=str(data["version"]),
        standard_id=data["standard_id"],
        policy_ids=list(data["policy_ids"]),
        owner=data["owner"],
        harness_support=list(data["harness_support"]),
        telemetry_emit=str(tel.get("emit", True)).lower() not in ("false", "0", "no"),
        success_signal=signal,
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        path=path,
        extra={k: v for k, v in data.items() if k not in known},
    )


def load_manifest(path: Path) -> SkillManifest:
    return parse_manifest(path.read_text(encoding="utf-8"), path=path)


def iter_skill_files(root: Path) -> list[Path]:
    """Every SKILL.md under root, sorted, following symlinked directories.

    Catalogues are assembled by symlinking skills into a shared directory
    (~/.claude/skills), which Path.rglob would silently refuse to descend into.
    Each resolved directory is visited once, so a symlink cycle cannot hang a hook.
    """
    found, seen = [], set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []          # cycle, or a second route to the same tree
            continue
        seen.add(real)
        if "SKILL.md" in filenames:
            found.append(Path(dirpath) / "SKILL.md")
    return sorted(found)


def load_catalogue(root: Path, strict: bool = True) -> dict[str, SkillManifest]:
    """All SKILL.md files under root, keyed by skill name.

    `strict` is the CI contract gate: any invalid or duplicate manifest raises.
    Hooks pass strict=False so one broken SKILL.md in a shared skills directory
    cannot silence telemetry for every other skill (first definition wins).
    """
    out: dict[str, SkillManifest] = {}
    problems: list[str] = []
    for p in iter_skill_files(root):
        try:
            m = load_manifest(p)
        except (ManifestError, OSError, yaml.YAMLError) as e:
            # Every failure, not the first. A gate that reports one problem per
            # run turns onboarding ten skills into ten runs whose output all
            # looks the same (#49).
            problems.append(str(e) if isinstance(e, ManifestError) else f"{p}: {e}")
            continue
        if m.name in out:
            problems.append(f"{p}: duplicate skill name {m.name!r}, already defined in "
                            f"{out[m.name].path}")
            continue
        out[m.name] = m
    if problems and strict:
        raise ManifestError("\n".join(problems))
    return out


def cli(argv: list[str] | None = None) -> int:
    """The CI contract gate.

    Exit 0 valid, 1 an invalid manifest, 2 the root cannot be read. That last
    case used to exit 0 with "0 skill(s) valid" — a typo'd path in CI reported a
    clean gate over nothing, and `--help` was parsed as a directory name.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="stdtel-validate",
        description="Validate SKILL.md front-matter against the standards contract.")
    ap.add_argument("root", nargs="?", default="skills",
                    help="directory to scan recursively for SKILL.md (default: skills)")
    ap.add_argument("--quiet", "-q", action="store_true", help="only report failures")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(a.root).expanduser()
    if not root.is_dir():
        print(f"stdtel-validate: no such directory: {root}", file=sys.stderr)
        return 2
    try:
        cat = load_catalogue(root)
    except ManifestError as e:
        # One line per failing manifest, each naming its file. --quiet suppresses
        # the passes, never the failures: the failures are the point.
        for line in str(e).splitlines():
            print(f"INVALID  {line}", file=sys.stderr)
        return 1
    if not a.quiet:
        for name, m in cat.items():
            print(f"OK  {name}@{m.version}  {m.standard_id}  policies={len(m.policy_ids)}")
    print(f"{len(cat)} skill(s) valid in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
