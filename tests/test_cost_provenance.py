"""Cost provenance on the external contract (issue #88), and council's feedback.

A run can mix billed and estimated calls, and council emits one span per run. A
single provenance label over one amount has no honest value for that run: either
the estimate contaminates the billed figure, or it stays invisible. So the amount
is split — `cost_usd` keeps meaning *observed*, `cost_estimated_usd` carries what
was priced from a list — and `cost_source` labels the observed part.
"""
from __future__ import annotations

import collections
import importlib.util
import json
from pathlib import Path

import pytest

from stdtel import artefact, conform

ROOT = Path(__file__).resolve().parent.parent


def loader():
    spec = importlib.util.spec_from_file_location("load_traces_cp", ROOT / "warehouse" / "load_traces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ext(**kw):
    base = dict(system="llm-council", operation="consult", started_at=1.0, ended_at=2.0)
    return artefact.external(**{**base, **kw})["attributes"]


# --- the builder ---------------------------------------------------------------

def test_the_vocabulary_is_generic_not_one_emitters_internals():
    """`registry_estimate` and `local_zero` are how council names its code paths.
    A contract other emitters adopt must not carry one emitter's vocabulary."""
    assert artefact.COST_SOURCES == ("provider", "local")


def test_an_estimate_travels_beside_the_observed_cost_never_inside_it():
    a = ext(cost_usd=1.25, cost_source="provider", cost_estimated_usd=0.40)
    assert a["std.external.cost_usd"] == 1.25
    assert a["std.external.cost_estimated_usd"] == 0.40
    assert a["std.external.cost_source"] == "provider"


def test_an_estimate_only_run_carries_no_observed_cost():
    """The run the contract could not carry before: every call priced from a list.
    It must arrive with its estimate and *without* `cost_usd`, or coverage
    against the invoice would count a guess as a bill."""
    a = ext(cost_estimated_usd=0.40)
    assert "std.external.cost_usd" not in a
    assert "std.external.cost_source" not in a
    assert a["std.external.cost_estimated_usd"] == 0.40


def test_an_unobserved_estimate_is_omitted_and_an_observed_zero_is_kept():
    assert "std.external.cost_estimated_usd" not in ext(cost_usd=1.0, cost_source="provider")
    assert ext(cost_estimated_usd=0.0)["std.external.cost_estimated_usd"] == 0.0


def test_a_label_on_nothing_is_refused():
    """A provenance with no observed amount describes a figure that does not exist."""
    with pytest.raises(ValueError, match="cost_source"):
        ext(cost_source="provider")


def test_a_source_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError, match="cost_source"):
        ext(cost_usd=1.0, cost_source="registry_estimate")


def test_an_observed_cost_without_a_label_is_still_accepted():
    """Additive change: council's spans from before this contract version carry
    `cost_usd` with no label, and they must keep loading. Unlabelled is its own
    category in query 7, never silently counted as `provider`."""
    a = ext(cost_usd=1.0)
    assert "std.external.cost_source" not in a


# --- the checker -----------------------------------------------------------------

def span(**attrs):
    base = {"std.artefact.kind": "external", "std.external.system": "llm-council",
            "std.external.operation": "consult", "std.artefact.source": "emitter"}
    kv = []
    for k, v in {**base, **attrs}.items():
        if v is None:
            kv.append({"key": k, "value": {}})
        elif isinstance(v, bool):
            kv.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, (int, float)):
            kv.append({"key": k, "value": {"doubleValue": v}})
        else:
            kv.append({"key": k, "value": {"stringValue": v}})
    return {"name": "std.artefact.activation", "attributes": kv}


def payload(*spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def problems(*spans):
    return [p.message for p in conform.check(payload(*spans)).problems]


def test_conform_accepts_the_full_split():
    assert problems(span(**{"std.external.cost_usd": 1.0, "std.external.cost_source": "provider",
                             "std.external.cost_estimated_usd": 0.3})) == []


def test_conform_refuses_a_council_internal_label():
    msgs = problems(span(**{"std.external.cost_usd": 1.0,
                            "std.external.cost_source": "registry_estimate"}))
    assert any("cost_source" in m and "provider" in m for m in msgs)


def test_conform_refuses_a_label_with_no_observed_cost():
    msgs = problems(span(**{"std.external.cost_source": "provider",
                            "std.external.cost_estimated_usd": 0.3}))
    assert any("cost_source" in m and "no observed" in m for m in msgs), msgs
    assert not any("allowlist" in m for m in msgs), "must fail for the reason, not as a stray key"


def test_conform_refuses_a_null_or_string_estimate():
    null = problems(span(**{"std.external.cost_estimated_usd": None}))
    text = problems(span(**{"std.external.cost_estimated_usd": "0.3"}))
    assert any("cost_estimated_usd" in m and "omit" in m for m in null), null
    assert any("cost_estimated_usd" in m and "number" in m for m in text), text
    assert not any("allowlist" in m for m in null + text)


def test_an_estimate_does_not_count_as_a_reported_cost():
    """Coverage exists to reconcile against an invoice. An estimate-only run is
    better than silence, but it is not a bill, and counting it would let a file
    of guesses pass the thin-coverage warning."""
    r = conform.check(payload(span(**{"std.external.cost_estimated_usd": 0.3}),
                              span(**{"std.external.cost_usd": 1.0}),
                              span(**{"std.external.cost_usd": 1.0,
                                      "std.external.cost_estimated_usd": 0.2})))
    assert r.ok
    assert r.cost_reported == 2
    assert r.cost_estimated == 1, "a mixed run has an observed cost; it is not estimate-only"
    assert "estimate" in r.summary()


# --- the published contract, machine-readable -------------------------------------

def test_print_contract_emits_the_allowlist_and_its_version(capsys):
    assert conform.main(["--print-contract"]) == 0
    c = json.loads(capsys.readouterr().out)
    assert c["contract_version"] == conform.CONTRACT_VERSION == 2
    assert c["span_name"] == "std.artefact.activation"
    assert c["kind"] == "external"
    assert c["attributes"] == sorted(artefact.ALLOWED[artefact.KIND_EXTERNAL])
    assert c["cost_sources"] == list(artefact.COST_SOURCES)
    assert c["artefact_source"] == "emitter"


def test_the_contract_version_moves_when_the_contract_does():
    """A version nothing forces to change is a number that looks like evidence.
    Pinning the set here means an edit to the allowlist fails until someone
    decides whether the version moves, which is the decision that matters."""
    assert conform.CONTRACT_ATTRIBUTES_AT_VERSION == frozenset(artefact.ALLOWED[artefact.KIND_EXTERNAL])


def test_checking_a_file_still_requires_a_path(capsys):
    assert conform.main([]) == 2


# --- the loader ---------------------------------------------------------------------

def test_the_loader_knows_exactly_the_contract():
    """The loader is stdlib-only and cannot import the allowlist, so it keeps its
    own. If the two drift, a key the contract allows is silently not loaded —
    precisely the column of NULLs council asked to be able to see."""
    assert loader().LOADED_EXTERNAL_ATTRIBUTES == frozenset(artefact.ALLOWED[artefact.KIND_EXTERNAL])


def _tempo(*span_attrs):
    def kv(d):
        return [{"key": k, "value": ({"doubleValue": v} if isinstance(v, float) else {"stringValue": v})}
                for k, v in d.items()]
    spans = [{"name": "std.artefact.activation", "spanId": "AAAAAAAAAAE=", "traceId": "AAAAAAAAAAAAAAAAAAAAAQ==",
              "startTimeUnixNano": "1000000000", "endTimeUnixNano": "2000000000", "attributes": kv(a)}
             for a in span_attrs]
    return {"batches": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": spans}]}]}


def test_the_loader_names_the_keys_it_did_not_load():
    """Tempo keeps an unknown attribute; the loader is where it disappears. So the
    loader is where it has to be counted — by key, because a bare count tells an
    emitter something is wrong without telling it what."""
    mod = loader()
    unknown = collections.Counter()
    acts, _, _ = mod.collect({"t": 1}, lambda _: _tempo(
        {"std.artefact.kind": "external", "std.external.system": "c", "std.external.requests_made": "3"},
        {"std.artefact.kind": "external", "std.external.system": "c", "std.external.requests_made": "4"},
        {"std.artefact.kind": "turn", "std.prompt.id": "p", "some.other.key": "x"},
    ), unknown=unknown)
    assert len(acts) == 3
    assert unknown == {"std.external.requests_made": 2}, "only external spans come from outside this repo"


def test_the_loader_reads_the_split_cost():
    mod = loader()
    acts, _, _ = mod.collect({"t": 1}, lambda _: _tempo(
        {"std.artefact.kind": "external", "std.external.system": "c",
         "std.external.cost_usd": 1.5, "std.external.cost_source": "provider",
         "std.external.cost_estimated_usd": 0.25},
        {"std.artefact.kind": "external", "std.external.system": "c"},
    ))
    full, bare = acts
    assert (full["external_cost_usd"], full["external_cost_source"], full["external_cost_estimated_usd"]) == (1.5, "provider", 0.25)
    assert (bare["external_cost_usd"], bare["external_cost_source"], bare["external_cost_estimated_usd"]) == (None, None, None)


def test_loader_run_records_what_it_could_not_load():
    mod = loader()
    assert {"unknown_attrs", "unknown_attr_keys"} <= set(mod.LOADER_RUN_COLS)
    schema = (ROOT / "warehouse" / "schema.sql").read_text()
    for col in ("unknown_attrs", "unknown_attr_keys", "external_cost_source", "external_cost_estimated_usd"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in schema, f"{col} needs an idempotent ALTER"


# --- query 7 ------------------------------------------------------------------------

Q7 = (ROOT / "warehouse" / "efficiency" / "07_external_spend_and_coverage.sql").read_text()


def test_query_seven_keeps_estimates_out_of_coverage_and_the_known_total():
    for col in ("cost_usd_estimated", "runs_estimated_only", "runs_unlabelled", "runs_outside_a_session"):
        assert col in Q7, col
    assert "coalesce(external_cost_usd" not in Q7.lower()
    assert "coalesce(external_cost_estimated_usd" not in Q7.lower()
    assert "%" not in Q7, "psycopg reads % as a placeholder, even in a comment"


def test_query_seven_says_an_empty_session_is_never_a_join_key():
    """`''` joins to `''`. Every no-session run would merge into one bucket."""
    assert "session_id = ''" in Q7 or "session_id <> ''" in Q7
    assert "never" in Q7.lower() and "join" in Q7.lower()


def test_the_coverage_warning_does_not_blame_capture_for_an_estimate(tmp_path, capsys):
    """An estimate-only run *did* capture something — the provider returned no
    figure. Telling that emitter to "fix cost capture" sends them to the wrong
    layer. The warning counts the two kinds of missing separately."""
    p = tmp_path / "s.json"
    p.write_text(json.dumps(payload(span(**{"std.external.cost_estimated_usd": 0.3}),
                                    span(),
                                    span(**{"std.external.cost_usd": 1.0}))))
    conform.main([str(p)])
    err = capsys.readouterr().err
    assert "2 of 3 report no observed cost" in err
    assert "1 of them carries an estimate" in err


def test_the_contract_points_at_the_decision_not_one_emitters_ticket():
    """The vocabulary refuses one emitter's internals; the published contract
    should not carry one emitter's closed ticket either."""
    changes = conform.contract()["changes"]
    assert "llm-council" not in changes
    assert "ADR-010" in changes
