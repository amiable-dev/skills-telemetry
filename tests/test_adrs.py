"""ADRs must record what was rejected and what the decision costs.

An ADR that only lists the chosen option is a changelog entry. The value is in
the rejected alternatives and the stated limitation, because those are what a
future contributor needs before re-litigating a decision.
"""
import re
from pathlib import Path

import pytest
import yaml

ADRS = sorted((Path(__file__).resolve().parent.parent / "docs" / "adrs").glob("*.md"))
REQUIRED_SECTIONS = ("Context", "Options considered", "Decision", "Consequences")


def front_matter(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---")[1])


def sections(path: Path) -> list[str]:
    return re.findall(r"^## (.+)$", path.read_text(), re.M)


def test_adrs_exist():
    assert len(ADRS) >= 5


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.stem)
def test_front_matter_is_complete(adr):
    fm = front_matter(adr)
    for key in ("title", "status", "date", "tags"):
        assert key in fm, f"missing {key}"
    assert fm["status"] in {"proposed", "accepted", "superseded", "rejected"}
    assert fm["title"].startswith(f"ADR-{adr.name[:3]}"), "title must carry its own number"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.stem)
def test_house_sections_present(adr):
    found = sections(adr)
    for required in REQUIRED_SECTIONS:
        assert any(required in s for s in found), f"missing '## {required}'"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.stem)
def test_records_what_was_rejected(adr):
    """The chosen option alone is a changelog; the rejected ones are the reasoning."""
    body = adr.read_text().split("## Options considered")[1].split("## Decision")[0]
    assert body.lower().count("rejected") >= 3, "an ADR should weigh real alternatives"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.stem)
def test_states_a_known_limitation(adr):
    """Every decision costs something; an ADR that claims none is not finished."""
    text = adr.read_text()
    assert "Known limitations" in text or "unverified" in text.lower(), \
        "state what this decision gives up"


@pytest.mark.parametrize("adr", ADRS, ids=lambda p: p.stem)
def test_relative_links_resolve(adr):
    """A dead cross-reference is worse than none — it implies the reasoning is written down."""
    for target in re.findall(r"\]\((\.\./[^)#]+|\d{3}-[^)#]+)\)", adr.read_text()):
        assert (adr.parent / target).exists(), f"broken link: {target}"


def test_numbers_are_unique_and_contiguous():
    numbers = sorted(int(p.name[:3]) for p in ADRS)
    assert numbers == list(range(1, len(numbers) + 1)), f"gap or duplicate: {numbers}"
