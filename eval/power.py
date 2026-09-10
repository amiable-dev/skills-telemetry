"""Sample-size arithmetic for the skill/harness evaluation design.

Every figure in docs/evaluation-power.md is produced here, so the numbers can be
re-derived rather than trusted. `--check` fails if the committed doc has drifted.

Two different questions, two different tests:

  1. Does a skill raise the FIRST-TIME POLICY PASS RATE? A proportion, compared
     between a with-skill and a without-skill arm.
  2. Does a harness or skill change TOKENS PER MERGED PR? A continuous, strongly
     right-skewed quantity, compared within developer under a crossover.

The second needs a design effect: PRs from one developer resemble each other, so
they carry less information than the same number of PRs from different people.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from statistics import NormalDist

DOC = Path(__file__).resolve().parent.parent / "docs" / "evaluation-power.md"

# The one sentence that must read identically everywhere it appears: README, the
# scorecard agent, stdtel-query and the power doc. A floor stated three different
# ways is three different floors, and the loosest one wins in practice.
HARD_FLOOR = ("below 30 merged PRs per arm, or fewer than 5 developers, report "
              "descriptively and make no comparative claim")
FLOOR_SURFACES = ("README.md", "docs/evaluation-power.md",
                  "agents/skill-scorecard-analyst.md", "skills/stdtel-query/SKILL.md")
ALPHA, POWER = 0.05, 0.80


def z(p: float) -> float:
    return NormalDist().inv_cdf(p)


def _zsum(alpha: float, power: float) -> float:
    return z(1 - alpha / 2) + z(power)


def n_two_proportion(p1: float, p2: float, alpha: float = ALPHA, power: float = POWER) -> int:
    """Per-arm PRs to detect p1 -> p2, two independent proportions, two-sided.

        n = (z_{1-a/2} + z_{1-b})^2 * [p1(1-p1) + p2(1-p2)] / (p1 - p2)^2
    """
    if p1 == p2:
        raise ValueError("no effect to detect")
    return math.ceil(_zsum(alpha, power) ** 2
                     * (p1 * (1 - p1) + p2 * (1 - p2)) / (p1 - p2) ** 2)


def design_effect(prs_per_dev: int, icc: float) -> float:
    """DEFF = 1 + (m-1) * ICC. Clustering by developer inflates the requirement."""
    return 1 + (prs_per_dev - 1) * icc


def n_token_ratio(cv: float, rel_effect: float, icc: float, prs_per_dev: int,
                  alpha: float = ALPHA, power: float = POWER) -> int:
    """Per-arm PRs to detect a fractional change in mean tokens per PR.

        n_iid = (z_{1-a/2} + z_{1-b})^2 * 2*CV^2 / effect^2      then  x DEFF

    Working on the ratio scale means the standard deviation enters as the
    coefficient of variation, so no absolute token count is needed.
    """
    n_iid = _zsum(alpha, power) ** 2 * 2 * cv ** 2 / rel_effect ** 2
    return math.ceil(n_iid * design_effect(prs_per_dev, icc))


def n_paired_by_developer(cv: float, rel_effect: float, rho: float = 0.5,
                          alpha: float = ALPHA, power: float = POWER) -> int:
    """Pairs (developer-periods) when each developer contributes ONE mean per arm.

    Kept only to show why this framing is wrong for our data: collapsing a
    developer's PRs to a single mean discards the within-developer variation the
    crossover exists to exploit, and overstates what is needed by roughly 5x.
    """
    sd_diff = cv * math.sqrt(2 * (1 - rho))
    return math.ceil(_zsum(alpha, power) ** 2 * sd_diff ** 2 / rel_effect ** 2)


# --- table rendering -------------------------------------------------------

def table_pass_rate() -> str:
    rows = [(0.60, 0.80), (0.60, 0.75), (0.60, 0.70), (0.70, 0.85), (0.70, 0.80), (0.80, 0.90)]
    out = [f"*alpha={ALPHA}, power={POWER:.0%}, two-sided, independent arms.*", "",
           "| baseline | with skill | absolute lift | PRs per arm |",
           "|---|---|---|---|"]
    for p1, p2 in rows:
        out.append(f"| {p1:.0%} | {p2:.0%} | {p2 - p1:+.0%} | **{n_two_proportion(p1, p2)}** |")
    return "\n".join(out)


def table_token_ratio() -> str:
    iccs = (0.1, 0.2, 0.4)
    out = [f"*alpha={ALPHA}, power={POWER:.0%}, CV=0.9, m=10 PRs per developer per arm.*", "",
           "| effect to detect | " + " | ".join(f"ICC={i}" for i in iccs) + " |",
           "|---|" + "---|" * len(iccs)]
    for eff in (0.40, 0.30, 0.20, 0.10):
        cells = " | ".join(str(n_token_ratio(0.9, eff, i, 10)) for i in iccs)
        out.append(f"| {eff:.0%} | {cells} |")
    return "\n".join(out)


def table_cv_sensitivity() -> str:
    cvs = (0.6, 0.9, 1.2)
    out = ["*Same as above at ICC=0.2, varying only the coefficient of variation.*", "",
           "| effect to detect | " + " | ".join(f"CV={c}" for c in cvs) + " |",
           "|---|" + "---|" * len(cvs)]
    for eff in (0.40, 0.30, 0.20):
        cells = " | ".join(str(n_token_ratio(c, eff, 0.2, 10)) for c in cvs)
        out.append(f"| {eff:.0%} | {cells} |")
    return "\n".join(out)


def table_calendar() -> str:
    need = n_token_ratio(0.9, 0.30, 0.2, 10)
    out = [f"*A 30% token effect at CV=0.9, ICC=0.2 needs **{need} PRs per arm**. "
           f"One crossover cycle = one fortnight per arm = 4 weeks.*", "",
           "| team | PRs/dev/fortnight | PRs per arm per cycle | cycles | calendar |",
           "|---|---|---|---|---|"]
    for devs in (4, 8, 15, 30):
        for prs in (3, 5):
            per_cycle = devs * prs
            cycles = math.ceil(need / per_cycle)
            out.append(f"| {devs} devs | {prs} | {per_cycle} | {cycles} | ~{cycles * 4} weeks |")
    return "\n".join(out)


def table_correction() -> str:
    out = ["*Why the first attempt was wrong: pairing at developer level versus "
           "PR level with developer as a blocking factor, both for a 30% effect at CV=0.9.*", "",
           "| framing | unit of analysis | n required | in PRs (10 per dev per arm) |",
           "|---|---|---|---|"]
    pairs = n_paired_by_developer(0.9, 0.30)
    prs = n_token_ratio(0.9, 0.30, 0.2, 10)
    out.append(f"| paired by developer (**wrong**) | developer-periods | {pairs} pairs "
               f"| {pairs * 10} |")
    out.append(f"| PR-level, developer as block | PRs | {prs} PRs | {prs} |")
    return "\n".join(out)


TABLES = {
    "pass-rate": table_pass_rate,
    "token-ratio": table_token_ratio,
    "cv-sensitivity": table_cv_sensitivity,
    "calendar": table_calendar,
    "correction": table_correction,
}
BLOCK = re.compile(r"<!-- generated:(?P<name>[\w-]+) -->.*?<!-- /generated -->", re.S)


def render(doc_text: str) -> tuple[str, list[str]]:
    """Fill every generated block; returns (text, names filled).

    The caller checks that every table was placed. An earlier version reported
    success by counting table *definitions* rather than substitutions, so a regex
    that matched nothing still printed "regenerated 5 tables" and left the
    document empty.
    """
    filled: list[str] = []

    def swap(m):
        name = m.group("name")
        if name not in TABLES:
            raise SystemExit(f"unknown generated block: {name!r}")
        filled.append(name)
        return f"<!-- generated:{name} -->\n{TABLES[name]()}\n<!-- /generated -->"

    return BLOCK.sub(swap, doc_text), filled


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="regenerate the tables in docs/evaluation-power.md")
    ap.add_argument("--check", action="store_true", help="fail if the doc is out of date")
    a = ap.parse_args(argv)
    current = DOC.read_text()
    updated, filled = render(current)
    missing = sorted(set(TABLES) - set(filled))
    if missing:
        print(f"no placeholder found for: {', '.join(missing)}", file=sys.stderr)
        return 1
    if a.check:
        if current != updated:
            print("docs/evaluation-power.md is stale; run `mise run power`", file=sys.stderr)
            return 1
        print(f"evaluation-power.md is up to date ({len(filled)} tables verified)")
        return 0
    DOC.write_text(updated)
    print(f"wrote {len(filled)} table(s) into {DOC.name}: {', '.join(filled)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
