"""Does a foreign emitter's output meet the external-spend contract?

stdtel *observes* skills, sub-agents, compactions and turns: it owns the code
that produces them, so the per-kind allowlist is enforced at the point of
construction. External spend is different. It is reported by a process stdtel
does not control, in a repository it does not own, and the only enforcement
available is a gate that the emitter's own CI can run. Without one the contract
in ADR-010 is a document rather than an agreement.

The shape of this check comes from a measured failure rather than from theory.
A real emitter records no cost at all for roughly two thirds of its calls. A
checker that validated only the shape of a span would have passed that file
happily, and the warehouse would have averaged over the holes. So coverage is
part of the report, and a thin file is called out even when nothing is
technically wrong.

    stdtel-conform spans.json        # or: ... | stdtel-conform -

The input is OTLP JSON — what an OTLP/HTTP exporter posts (`resourceSpans`) or
what Tempo returns (`batches`). Both keys are accepted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from stdtel.artefact import ALLOWED, KIND_EXTERNAL, KINDS, SPAN_NAME

#: What the harness exports, and therefore what an emitter should be copying.
#: Anything else joins to nothing, which is worse than carrying no id at all.
SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: Checked explicitly as well as by the allowlist, so the failure says *why*
#: rather than "unknown attribute". Metadata only is the project's first rule.
CONTENT_PREFIXES = ("gen_ai.input", "gen_ai.output", "gen_ai.prompt", "gen_ai.completion",
                    "tool.input", "tool.output", "tool.arguments", "tool.result",
                    "std.external.prompt", "std.external.question", "std.external.args")

#: Below this share of external spans carrying a cost, the report says so even
#: when every span is individually valid.
THIN = 0.9


@dataclass
class Problem:
    span_index: int | None
    message: str


@dataclass
class Report:
    spans: int = 0
    external: int = 0
    cost_reported: int = 0
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def thin_cost_coverage(self) -> bool:
        return self.external > 0 and (self.cost_reported / self.external) < THIN

    def summary(self) -> str:
        if not self.external:
            return f"{self.spans} span(s) checked; none reported external spend"
        pct = self.cost_reported / self.external
        return (f"{self.spans} span(s) checked; {self.external} external; "
                f"cost reported on {self.cost_reported} of {self.external} ({pct:.0%})")


def _value(raw: dict):
    """One OTLP attribute value, or None when the emitter sent an empty one.

    An empty `value` object is an emitter saying "this attribute exists and is
    nothing", which is exactly the distinction ADR-005 turns on. It must not be
    read as absent, or the contract cannot tell an unreported cost from a null.
    """
    if not raw:
        return None
    return list(raw.values())[0]


def _attrs(items: list) -> dict:
    return {kv["key"]: _value(kv.get("value") or {}) for kv in items or []}


def _spans(payload: dict):
    for batch in (payload.get("resourceSpans") or payload.get("batches") or []):
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            yield from scope.get("spans") or []


def check_span(index: int, span: dict, report: Report) -> None:
    problems = report.problems
    if span.get("name") != SPAN_NAME:
        problems.append(Problem(index, f"span name is {span.get('name')!r}; the contract is "
                                       f"{SPAN_NAME!r} — one name discriminated by kind"))
        return
    attrs = _attrs(span.get("attributes"))
    kind = attrs.get("std.artefact.kind")
    if kind not in KINDS:
        problems.append(Problem(index, f"std.artefact.kind is {kind!r}; the closed set is "
                                       f"{list(KINDS)}"))
        return

    for key in attrs:
        if key.startswith(CONTENT_PREFIXES):
            problems.append(Problem(index, f"{key} carries content; this project records metadata "
                                           f"only, and an emitter's payload is not ours to scrub"))
    stray = sorted(k for k in attrs if k not in ALLOWED[kind] and not k.startswith(CONTENT_PREFIXES))
    for key in stray:
        problems.append(Problem(index, f"{key} is not in the allowlist for kind={kind}; it would be "
                                       f"dropped downstream and you would never learn why"))

    sid = attrs.get("session.id")
    if sid is not None and not SESSION_ID.match(str(sid)):
        problems.append(Problem(index, f"session.id {sid!r} is not the format the harness exports; "
                                       f"it would join to nothing. Omit it rather than invent one"))

    if kind != KIND_EXTERNAL:
        return
    report.external += 1
    if not attrs.get("std.external.system"):
        problems.append(Problem(index, "std.external.system is required: spend with no emitter "
                                       "named inflates a total nobody can trace back"))
    if "std.external.cost_usd" in attrs:
        cost = attrs["std.external.cost_usd"]
        if cost is None:
            problems.append(Problem(index, "std.external.cost_usd is null — omit the attribute "
                                           "instead. A null reads as a cost of nothing; an absent "
                                           "attribute records that none was observed"))
        elif isinstance(cost, bool) or not isinstance(cost, (int, float)):
            problems.append(Problem(index, f"std.external.cost_usd is {type(cost).__name__}; it "
                                           f"must be a number, not a string"))
        else:
            report.cost_reported += 1


def check(payload: dict) -> Report:
    report = Report()
    spans = list(_spans(payload))
    report.spans = len(spans)
    if not spans:
        report.problems.append(Problem(None, "no spans in this payload — a gate that passes an "
                                             "empty file checks nothing"))
        return report
    for i, span in enumerate(spans):
        check_span(i, span, report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stdtel-conform", description=__doc__.splitlines()[0])
    ap.add_argument("path", help="OTLP JSON file, or - for stdin")
    a = ap.parse_args(argv)

    try:
        raw = sys.stdin.read() if a.path == "-" else Path(a.path).read_text()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        print(f"stdtel-conform: cannot read {a.path}: {e}", file=sys.stderr)
        return 2

    report = check(payload)
    print(report.summary())
    for p in report.problems:
        where = f"span {p.span_index}: " if p.span_index is not None else ""
        print(f"stdtel-conform: {where}{p.message}", file=sys.stderr)
    if report.ok and report.thin_cost_coverage:
        missing = report.external - report.cost_reported
        print(f"stdtel-conform: every span is valid, but {missing} of {report.external} report no "
              f"cost. A total over this file is an average of the part that was measured, not of "
              f"the work that was done, and it will not reconcile against a provider's invoice — "
              f"fix the emitter's cost capture before trusting the number.", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
