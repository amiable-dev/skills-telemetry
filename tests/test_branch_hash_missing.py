"""#122: a session with no repository has no branch identity, and the warehouse
must say so with NULL — ADR-005's "missing data is its own category".

The capture side sends `std.branch.hash=""` deliberately, so repository-less
sessions do not share one real-looking hash. The loader copied that through, and
'' became a second spelling of "unknown" that `branch_hash IS NULL` does not count.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DSN = "postgresql://postgres:stdtel@localhost:5432/stdtel"
SPAN = {"spanId": "s", "traceId": "t", "startTimeUnixNano": "1", "endTimeUnixNano": "2"}


def lt():
    spec = importlib.util.spec_from_file_location("lt_bh", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("parser", ["parse_activation", "parse_span", "parse_session"])
@pytest.mark.parametrize("sent", ["", None])
def test_no_branch_identity_is_null(parser, sent):
    attrs = {"std.artefact.kind": "skill", "session.id": "x"}
    if sent is not None:
        attrs["std.branch.hash"] = sent
    row = getattr(lt(), parser)(attrs, {}, SPAN)
    assert row["branch_hash"] is None


@pytest.mark.parametrize("parser", ["parse_activation", "parse_span", "parse_session"])
def test_a_real_hash_is_kept(parser):
    row = getattr(lt(), parser)({"std.artefact.kind": "skill", "std.branch.hash": "abc123"}, {}, SPAN)
    assert row["branch_hash"] == "abc123"


def test_rows_already_stored_as_empty_become_null():
    """The schema's migration, against a real Postgres. Scoped to '' and to this
    test's own rows for the assertion; the UPDATE itself touches only ''."""
    try:
        import psycopg
        conn = psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:                           # noqa: BLE001
        pytest.skip(f"postgres unavailable: {str(e)[:80]}")
    schema = (ROOT / "warehouse" / "schema.sql").read_text()
    with conn, conn.cursor() as cur:
        cur.execute(schema)
        cur.execute("DELETE FROM session_cost WHERE session_id = 'wtest-bh'")
        cur.execute("INSERT INTO session_cost (session_id, branch_hash) VALUES ('wtest-bh', '')")
        cur.execute(schema)
        cur.execute("SELECT branch_hash FROM session_cost WHERE session_id = 'wtest-bh'")
        got = cur.fetchone()[0]
        cur.execute("DELETE FROM session_cost WHERE session_id = 'wtest-bh'")
    conn.close()
    assert got is None
