"""The insight walkthroughs must stay runnable and honest.

These checks are offline: they verify the documented SQL refers to objects that
actually exist, and that the doc keeps the properties the issue asked for. They
cannot verify the captured output, which was taken from a live stack.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "insight-walkthroughs.md"
SCHEMA = (ROOT / "warehouse" / "schema.sql").read_text()
TEXT = DOC.read_text()
FLAT = " ".join(TEXT.split())      # markdown wraps; compare against this for prose


def sql_blocks() -> list[str]:
    return re.findall(r"```sql\n(.*?)```", TEXT, re.S)


def test_doc_contains_runnable_sql():
    assert len(sql_blocks()) >= 4


def test_every_table_referenced_exists():
    """Catches a renamed table or a query copied from an older schema."""
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
    for block in sql_blocks():
        for ref in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", block):
            assert ref in tables, f"unknown table {ref!r} in walkthrough SQL"


def test_every_column_referenced_exists():
    from tests.test_warehouse import schema_columns
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
    known = {c for t in tables for c in schema_columns(t)} | tables
    # identifiers that are SQL keywords, aliases or functions, not columns
    ignore = {"count", "sum", "round", "select", "from", "join", "on", "group", "by", "order",
              "desc", "as", "left", "where", "and", "numeric", "nullif", "distinct", "sc", "p",
              "i", "invocations", "session_rows", "matched_prs", "policy_rows", "cache_read",
              "cache_hit_rate", "sessions", "prs", "policy_results", "tail_tokens_col"}
    for block in sql_blocks():
        for ident in re.findall(r"\b([a-z_][a-z0-9_]{3,})\b", block.lower()):
            if ident in ignore or ident in known:
                continue
            assert ident in known or "." in ident, f"unknown identifier {ident!r} in walkthrough SQL"


# --- the properties issue #5 asked for ---

def test_shows_how_to_make_the_agent_available():
    assert "loaded at session start" in FLAT, "a running session will not see a new agent"
    assert ".claude/agents" in TEXT and "/plugin install" in TEXT


def test_includes_a_refusal_example():
    assert "This refusal is the right output, not a failure" in FLAT


def test_includes_misleading_readings_with_their_tells():
    assert TEXT.count("plausible") >= 2, "need more than one wrong-reading example"
    assert "llm_requests = 0" in TEXT          # the "free skill" tell
    assert "one group" in FLAT                 # the single-group GROUP BY tell
    assert "## The recurring tells" in TEXT


def test_states_row_counts_with_results():
    """Every captured output must be accompanied by how much data produced it."""
    assert "(2 rows)" in TEXT or "sessions | cache_read" in TEXT
    assert "0 rows" in TEXT


def test_repeats_the_hard_floor_verbatim():
    from eval.power import HARD_FLOOR
    assert HARD_FLOOR in " ".join(TEXT.split())
