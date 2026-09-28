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

from stdtel.artefact import ALLOWED, COST_SOURCES, KIND_EXTERNAL, KINDS, SOURCE_EMITTER, SPAN_NAME

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

#: Bumped on every change to the external contract. 1 was the original set;
#: 2 added `cost_source` and `cost_estimated_usd` (issue #88); 3 added
#: `tool_use_id` and `requests_unpriced` (ADR-012, issue #94).
#:
#: Additive changes bump it too. An emitter comparing its hand copy against
#: `--print-contract` should learn that something exists that it does not send,
#: not only that something it sends has gone.
CONTRACT_VERSION = 3

#: The attribute set CONTRACT_VERSION describes, written out so that editing the
#: allowlist fails a test until someone decides whether the version moves.
CONTRACT_ATTRIBUTES_AT_VERSION = frozenset({
    "std.artefact.kind", "std.artefact.name", "std.artefact.source",
    "std.external.system", "std.external.operation", "std.external.cost_usd",
    "std.external.requests", "std.external.duration_ms",
    "std.external.cost_source", "std.external.cost_estimated_usd",
    "std.external.tool_use_id", "std.external.requests_unpriced",
    "gen_ai.operation.name", "gen_ai.request.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_creation_input_tokens",
    "session.id",
    "std.scope.name", "std.scope.key", "std.scope.id", "std.scope.source",
})


def contract() -> dict:
    """The external contract as data, for an emitter to diff its own copy against.

    For *detecting* drift, not for generating the emitter's constant: both sides
    keep a longhand copy precisely so that neither compares a list with itself.
    An emitter that pulls this at build time lets a rename flow straight through.
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "span_name": SPAN_NAME,
        "kind": KIND_EXTERNAL,
        "artefact_source": SOURCE_EMITTER,
        "attributes": sorted(ALLOWED[KIND_EXTERNAL]),
        "cost_sources": list(COST_SOURCES),
        "changes": ("additive only, announced to adopting emitters before merge; "
                    "see ADR-010 decision 7 (docs/adrs/010-containment-and-scope.md)"),
    }


@dataclass
class Problem:
    span_index: int | None
    message: str


@dataclass
class Report:
    spans: int = 0
    external: int = 0
    cost_reported: int = 0
    cost_estimated: int = 0
    cost_partial: int = 0
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
        est = (f"; an estimate only on {self.cost_estimated}, not counted as reported"
               if self.cost_estimated else "")
        part = (f"; a lower bound on {self.cost_partial} (requests went unpriced), not counted as "
                f"reported" if self.cost_partial else "")
        return (f"{self.spans} span(s) checked; {self.external} external; "
                f"cost reported on {self.cost_reported} of {self.external} ({pct:.0%}){est}{part}")


def _value(raw: dict):
    """One OTLP attribute value, or None when the emitter sent an empty one.

    An empty `value` object is an emitter saying "this attribute exists and is
    nothing", which is exactly the distinction ADR-005 turns on. It must not be
    read as absent, or the contract cannot tell an unreported cost from a null.
    """
    if not raw:
        return None
    # Decode by the type key the emitter chose. OTLP JSON carries intValue as a
    # string, and a checker reading it raw would refuse every correct count
    # while accepting a stringValue "2", which is the emitter's mistake.
    if "intValue" in raw:
        try:
            return int(raw["intValue"])
        except (TypeError, ValueError):
            return raw["intValue"]
    if "arrayValue" in raw:
        return [_value(v) for v in (raw["arrayValue"] or {}).get("values") or []]
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
        problems.append(Problem(index, f"{key} is not in the allowlist for kind={kind}; the trace "
                                       f"store would keep it but the warehouse would never load "
                                       f"it, so every query would read it as absent"))

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
    src = attrs.get("std.artefact.source")
    if src is not None and src != SOURCE_EMITTER:
        problems.append(Problem(index, f"std.artefact.source is {src!r}; an external span is "
                                       f"{SOURCE_EMITTER!r}. `hook` and `transcript` say how stdtel "
                                       f"came by a value, and it came by this one from you"))
    unpriced = _count(attrs, "std.external.requests_unpriced", problems, index)
    if "std.external.tool_use_id" in attrs and not attrs["std.external.tool_use_id"]:
        problems.append(Problem(index, "std.external.tool_use_id is empty — omit it when the host "
                                       "gave you none; an empty id joins to nothing"))

    if "std.external.cost_usd" in attrs:
        cost = attrs["std.external.cost_usd"]
        if cost is None:
            problems.append(Problem(index, "std.external.cost_usd is null — omit the attribute "
                                           "instead. A null reads as a cost of nothing; an absent "
                                           "attribute records that none was observed"))
        elif isinstance(cost, bool) or not isinstance(cost, (int, float)):
            problems.append(Problem(index, f"std.external.cost_usd is {type(cost).__name__}; it "
                                           f"must be a number, not a string"))
        elif unpriced:
            # ADR-012: a lower bound, kept rather than discarded, and never
            # counted as reconciling to an invoice
            report.cost_partial += 1
        else:
            report.cost_reported += 1

    label = attrs.get("std.external.cost_source")
    if "std.external.cost_source" in attrs:
        if label not in COST_SOURCES:
            problems.append(Problem(index, f"std.external.cost_source is {label!r}; the vocabulary is "
                                           f"{list(COST_SOURCES)}. An estimate is not a source — send "
                                           f"it as std.external.cost_estimated_usd"))
        if "std.external.cost_usd" not in attrs:
            problems.append(Problem(index, "std.external.cost_source is set with no observed "
                                           "std.external.cost_usd: a provenance for a figure that "
                                           "does not exist"))

    if "std.external.cost_estimated_usd" in attrs:
        est = attrs["std.external.cost_estimated_usd"]
        if est is None:
            problems.append(Problem(index, "std.external.cost_estimated_usd is null — omit the "
                                           "attribute instead; nothing estimated and an estimate of "
                                           "nothing are different claims"))
        elif isinstance(est, bool) or not isinstance(est, (int, float)):
            problems.append(Problem(index, f"std.external.cost_estimated_usd is {type(est).__name__}; "
                                           f"it must be a number, not a string"))
        elif "std.external.cost_usd" not in attrs:
            report.cost_estimated += 1


def _count(attrs: dict, key: str, problems: list, index: int) -> int | None:
    """A non-negative integer attribute, or None when absent. `_value` has
    already decoded an OTLP intValue, so a string here is the emitter's mistake."""
    if key not in attrs:
        return None
    v = attrs[key]
    if isinstance(v, bool) or v is None:
        problems.append(Problem(index, f"{key} must be a non-negative integer count; omit it "
                                       f"when the emitter does not know"))
        return None
    if isinstance(v, int) and v >= 0:
        return v
    problems.append(Problem(index, f"{key} is {v!r}; it must be a non-negative integer count"))
    return None


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
    ap.add_argument("path", nargs="?", help="OTLP JSON file, or - for stdin")
    ap.add_argument("--print-contract", action="store_true",
                    help="print the external contract as JSON, for diffing an emitter's copy")
    a = ap.parse_args(argv)

    if a.print_contract:
        print(json.dumps(contract(), indent=2))
        return 0
    if not a.path:
        print("stdtel-conform: a path is required (or --print-contract)", file=sys.stderr)
        return 2

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
        est = (f" {report.cost_estimated} of them carries an estimate, so something was captured "
               f"but no provider figure was returned; the rest carry nothing."
               if report.cost_estimated else "")
        print(f"stdtel-conform: every span is valid, but {missing} of {report.external} report no "
              f"observed cost.{est} A total over this file is an average of the part that was "
              f"measured, not of the work that was done, and it will not reconcile against a "
              f"provider's invoice — find where the observed figure is lost before trusting the "
              f"number.", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
