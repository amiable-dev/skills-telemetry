"""#156: a compaction's reason and token counts, as the harness really records them.

Every compaction recorded on 2026-10-08 said `reason=unknown` with no tokens. The
PostCompact hook read `compaction_reason` and `token_count_estimate_*`, which no
real payload carries. A real one (captured 2026-10-13, Claude Code 2.1.296, in
tests/fixtures/hook_payloads.json) has `trigger` and a `compact_summary`, and no
token counts. The counts are in the transcript's `compact_boundary` entry:
`compactMetadata.trigger`, `preTokens`, `postTokens`.

`compact_summary` is content (a summary of the conversation) and is never read.
"""
import json
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from stdtel.hooks import cli as hooks
from stdtel.state import SessionState
from tests.test_artefact_capture import _entry, assistant, kinds, write_transcript

REAL = json.loads((Path(__file__).parent / "fixtures" / "hook_payloads.json").read_text())["PostCompact"]


def boundary(ts, trigger="auto", pre=806540, post=21713):
    """The 2026-10 shape, trimmed to the fields read."""
    return _entry(type="system", subtype="compact_boundary", timestamp=ts,
                  compactMetadata={"trigger": trigger, "preTokens": pre, "postTokens": post,
                                   "cumulativeDroppedTokens": 5361086, "durationMs": 55498})


def run(tmp_path, sid, payloads, lines):
    transcript = tmp_path / f"{sid}.jsonl"
    write_transcript(transcript, [assistant("2026-10-08T10:00:01.000Z"), *lines])
    hooks.session_start({"session_id": sid, "cwd": str(tmp_path)})
    for p in payloads:
        hooks.post_compact(dict(p, session_id=sid))
    exp = InMemorySpanExporter()
    hooks.stop({"session_id": sid, "transcript_path": str(transcript)}, exporter=exp)
    return [s.attributes for s in kinds(exp).get("compaction", [])]


def test_the_real_payload_and_its_boundary_give_the_reason_and_tokens(tmp_path):
    (c,) = run(tmp_path, "cc-1", [REAL], [boundary("2026-10-08T10:00:02.000Z", "manual", 32746, 4415)])
    assert c["std.compaction.reason"] == "manual"
    assert (c["std.compaction.tokens_before"], c["std.compaction.tokens_after"]) == (32746, 4415)
    assert c["std.artefact.source"] == "hook"


def test_the_reason_comes_from_the_payload_trigger(tmp_path):
    (c,) = run(tmp_path, "cc-2", [dict(REAL, trigger="auto")], [])
    assert c["std.compaction.reason"] == "auto"


def test_a_payload_without_a_trigger_takes_the_transcripts(tmp_path):
    payload = {k: v for k, v in REAL.items() if k != "trigger"}
    (c,) = run(tmp_path, "cc-3", [payload], [boundary("2026-10-08T10:00:02.000Z", "auto")])
    assert c["std.compaction.reason"] == "auto", "never unknown when the transcript has it"


def test_two_compactions_in_one_slice_pair_in_order(tmp_path):
    cs = run(tmp_path, "cc-4", [dict(REAL, trigger="auto"), dict(REAL, trigger="manual")],
             [boundary("2026-10-08T10:00:02.000Z", "auto", 900, 100),
              boundary("2026-10-08T10:00:03.000Z", "manual", 800, 50)])
    assert [(c["std.compaction.reason"], c["std.compaction.tokens_before"]) for c in cs] == \
        [("auto", 900), ("manual", 800)]


def test_a_compaction_with_no_boundary_omits_the_tokens(tmp_path):
    """Not zero: a compaction that dropped nothing would be a fabricated measurement."""
    (c,) = run(tmp_path, "cc-5", [REAL], [])
    assert "std.compaction.tokens_before" not in c and "std.compaction.tokens_after" not in c


def test_the_hook_is_still_the_only_source_once_it_has_fired(tmp_path):
    """The boundary enriches the hook's record; it is not a second compaction."""
    cs = run(tmp_path, "cc-6", [REAL], [boundary("2026-10-08T10:00:02.000Z", "manual")])
    assert len(cs) == 1


def test_the_summary_is_never_read_or_stored(tmp_path):
    marker = "SUMMARY-MARKER-156"
    run(tmp_path, "cc-7", [dict(REAL, compact_summary=marker)], [boundary("2026-10-08T10:00:02.000Z")])
    assert marker not in SessionState.load("cc-7").path.read_text()


def test_the_hook_reads_only_fields_the_real_payload_has():
    import inspect
    src = inspect.getsource(hooks.post_compact)
    for gone in ("compaction_reason", "token_count_estimate"):
        assert gone not in src, gone
    assert 'p.get("trigger")' in src
    assert 'p.get("compact_summary")' not in src and "p['compact_summary']" not in src


def test_the_payloads_trigger_wins_over_the_transcripts(tmp_path):
    """The hook is the observation; the boundary only fills what it lacks."""
    (c,) = run(tmp_path, "cc-8", [dict(REAL, trigger="auto")],
               [boundary("2026-10-08T10:00:02.000Z", "manual", 900, 100)])
    assert c["std.compaction.reason"] == "auto" and c["std.compaction.tokens_before"] == 900
