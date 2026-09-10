"""No dead relative links in the documentation.

A dead cross-reference is worse than none: it implies the reasoning is written
down somewhere. Two were introduced during this work and only caught by hand.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted(list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
              + list((ROOT / "skills").glob("*/SKILL.md")) + list((ROOT / "agents").glob("*.md")))
LINK = re.compile(r"\[[^\]]+\]\((?!https?:|mailto:|#)([^)]+)\)")


def test_docs_were_found():
    assert len(DOCS) > 8


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc):
    for target in LINK.findall(doc.read_text()):
        path = (doc.parent / target.split("#")[0]).resolve()
        assert path.exists(), f"{doc.relative_to(ROOT)} -> {target}"
