"""A synthetic fleet, so a newcomer can see what a populated scorecard looks like.

Two jobs. It shows what to look for before you have data of your own, and it is
the first thing ever to exercise `warehouse/scorecard.sql`, which until now has
only returned zero rows.

**Everything here is fictional and marked as such.** Tickets are `DEMO-*`, repos
are `demo-org/*`, teams are `demo-*`. This project exists to stop numbers looking
like measurements when they are not, and a seeded warehouse that reads as real
would be exactly that trap — so the marker is in every identifier, not a flag on
the side.

The picture is deliberately mixed rather than flattering: one skill with a clear
effect, one too thinly sampled to judge, and a share of unattributed and
unversioned rows, because those are what real data looks like early on.

    make demo          # load it
    make demo-clear    # remove every DEMO- row
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path

DEMO_MARKER = "DEMO"
TABLES = ("ticket", "pull_request", "policy_result", "skill_invocation", "session_cost",
          "defect", "artefact_activation")

#: Sub-agent types the demo fleet spawns, with how expensive each is per call.
#: Deliberately uneven: the point of the efficiency queries is that one artefact
#: usually dominates, and a flat distribution would hide that.
DEMO_SUBAGENTS = [("general-purpose", 3.0), ("Explore", 1.0), ("code-reviewer", 0.6)]

# one skill that helps, one barely used — so the demo shows a refusal as well as a finding
SKILLS = [
    {"name": "structured-logging", "version": "2.3.0", "standard_id": "STD-LOG-001",
     "policies": ["logging.required_fields", "logging.no_pii"], "weight": 0.75, "lift": 0.28},
    {"name": "stdtel-onboard", "version": "1.0.0", "standard_id": "STD-TEL-001",
     "policies": ["telemetry.manifest_valid"], "weight": 0.06, "lift": 0.0},
]
TEAMS = ["demo-payments", "demo-platform"]
DEVELOPERS = 8
WEEKS = 6


def _iso(when: dt.datetime) -> str:
    return when.replace(tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def generate(seed: int = 42, prs: int = 120) -> dict[str, list[dict]]:
    """Deterministic for a given seed, so docs and screenshots stay true."""
    rng = random.Random(seed)
    start = dt.datetime(2026, 7, 1)
    out: dict[str, list[dict]] = {t: [] for t in TABLES}

    for n in range(1, prs + 1):
        team = TEAMS[n % len(TEAMS)]
        dev = n % DEVELOPERS
        ticket_id = f"{DEMO_MARKER}-{100 + n}"
        pr_id = f"demo-org/demo-repo#{n}"
        opened = start + dt.timedelta(days=rng.uniform(0, WEEKS * 7), hours=rng.uniform(0, 8))
        cycle_hours = round(rng.lognormvariate(2.6, 0.7), 2)
        merged = opened + dt.timedelta(hours=cycle_hours)

        # crossover: alternating fortnights per developer, as the design prescribes
        fortnight = int((opened - start).days // 14)
        arm = "claude-code" if (dev + fortnight) % 2 == 0 else "none"

        out["ticket"].append({
            "ticket_id": ticket_id, "team": team, "story_points": rng.choice([1, 2, 3, 5]),
            "created_at": _iso(opened), "started_at": _iso(opened), "done_at": _iso(merged),
            "cycle_time_hours": cycle_hours, "lead_time_hours": cycle_hours,
            "harness_arm": arm,
        })
        out["pull_request"].append({
            "pr_id": pr_id, "repo": "demo-org/demo-repo", "ticket_id": ticket_id,
            "opened_at": _iso(opened), "merged_at": _iso(merged),
            "review_rounds": rng.choices([0, 1, 2, 3], weights=[45, 35, 15, 5])[0],
            "hours_to_first_approval": round(cycle_hours * rng.uniform(0.2, 0.8), 2),
            "ci_failures": rng.choices([0, 1, 2], weights=[70, 25, 5])[0],
            "assisted_by": arm,
        })

        skill = SKILLS[0] if rng.random() < SKILLS[0]["weight"] else SKILLS[1]
        used_skill = arm == "claude-code" and rng.random() < 0.85

        # first-time pass: the baseline, lifted when the relevant skill was used
        base = 0.58
        p_pass = min(0.95, base + (skill["lift"] if used_skill else 0.0))
        for policy_id in skill["policies"]:
            passed_first = rng.random() < p_pass
            out["policy_result"].append({
                "pr_id": pr_id, "policy_id": policy_id, "run_seq": 1,
                "passed": passed_first, "evaluated_at": _iso(merged - dt.timedelta(hours=1)),
            })
            if not passed_first:      # a fix, then a second CI run that passes
                out["policy_result"].append({
                    "pr_id": pr_id, "policy_id": policy_id, "run_seq": 2,
                    "passed": True, "evaluated_at": _iso(merged - dt.timedelta(minutes=20)),
                })

        session_id = f"demo-session-{n}"
        total_in = int(rng.lognormvariate(9.2, 0.6))
        total_out = int(total_in * rng.uniform(0.05, 0.2))
        # ~8% of sessions have no ticket key: a real and common data-quality fault
        sc_ticket = ticket_id if rng.random() > 0.08 else "unattributed"
        out["session_cost"].append({
            "session_id": session_id, "harness": "claude-code" if arm == "claude-code" else "none",
            "model": "claude-opus-5", "ticket_id": sc_ticket, "team": team,
            "input_tokens": total_in, "output_tokens": total_out,
            "cache_read_tokens": int(total_in * rng.uniform(0.4, 0.8)),
            "cache_creation_tokens": int(total_in * rng.uniform(0.05, 0.3)),
            "cost_usd": None, "active_seconds": int(cycle_hours * 60),
            "started_at": _iso(opened),
        })

        if used_skill:
            tail = int(total_in * rng.uniform(0.3, 0.7))
            # ~5% of invocations are of a skill the catalogue does not know
            catalogued = rng.random() > 0.05
            out["skill_invocation"].append({
                "span_id": f"demo{n:06d}", "trace_id": f"demotrace{n:06d}",
                "session_id": session_id, "started_at": _iso(opened),
                "ended_at": _iso(opened + dt.timedelta(seconds=rng.uniform(1, 40))),
                "harness": "claude-code", "harness_mode": "agent",
                "skill_name": skill["name"] if catalogued else "uncatalogued-helper",
                "invoked_as": skill["name"], "plugin": None,
                "skill_version": skill["version"] if catalogued else "unversioned",
                "standard_id": skill["standard_id"] if catalogued else None,
                "policy_ids": skill["policies"] if catalogued else [],
                "trigger": "direct", "model": "claude-opus-5",
                "load_tokens": rng.randint(200, 900), "tail_tokens": tail,
                "tail_tokens_first_only": tail, "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": int(total_in * 0.5), "cache_creation_tokens": int(total_in * 0.1),
                "llm_requests": rng.randint(1, 9), "is_error": rng.random() < 0.03,
                "ticket_id": sc_ticket, "repo": "demo-org/demo-repo", "team": team,
                "user_hash": f"demo-user-{dev}",
            })

        # --- ADR-009 artefact activations -------------------------------------
        # Without these `make demo` leaves every efficiency query empty, and a
        # newcomer following the docs sees four blank panels that look exactly
        # like a broken install. Same DEMO markers, same mixed picture.
        # ADR-010: roughly a third of the fleet is driven by a loop skill, so the
        # containment query has a mixed picture to show rather than one shape.
        # The scope key is the ticket, and the id changes with it, which is what
        # makes a per-iteration comparison possible at all.
        scoped = rng.random() < 0.35
        act = dict(session_id=session_id, harness="claude-code", harness_mode="agent",
                   repo="demo-org/demo-repo", team=team, ticket_id=sc_ticket,
                   user_hash=f"demo-user-{dev}",
                   scope_name="demo-epic-loop" if scoped else None,
                   scope_key=sc_ticket if scoped else None,
                   scope_id=f"demoscope-{n:06d}" if scoped else None,
                   scope_source=(rng.choice(["artefact", "overlay"]) if scoped else None))
        turns = rng.randint(2, 9)
        for turn_no in range(turns):
            at = opened + dt.timedelta(minutes=turn_no * rng.uniform(1, 12))
            turn_in = int(total_in / turns * rng.uniform(0.6, 1.4))
            hooks = {"cc-status": rng.randint(30, 90), "stdtel-hook": rng.randint(4, 40)}
            out["artefact_activation"].append({
                **act,
                "span_id": f"demoact-{n:06d}-t{turn_no}", "trace_id": f"demotrace{n:06d}",
                "started_at": _iso(at), "ended_at": _iso(at + dt.timedelta(seconds=rng.uniform(5, 300))),
                "kind": "turn", "name": None, "source": "transcript",
                "prompt_id": f"demo-prompt-{n}-{turn_no}", "parent_prompt_id": None,
                "model": "claude-opus-5", "input_tokens": turn_in,
                "output_tokens": int(turn_in * 0.1),
                "cache_read_tokens": int(turn_in * rng.uniform(1.5, 6.0)),
                "cache_creation_tokens": int(turn_in * rng.uniform(0.05, 0.3)),
                "llm_requests": rng.randint(1, 20), "tool_calls": rng.randint(0, 40),
                "duration_ms": int(rng.uniform(4_000, 400_000)), "is_error": False,
                "subagent_type": None, "subagent_id": None, "subagent_depth": None,
                "compaction_reason": None, "compaction_tokens_before": None,
                "compaction_tokens_after": None, "compaction_turns_since_previous": None,
                "hook_ms": sum(hooks.values()), "hook_ms_by_hook": json.dumps(hooks),
            })
        # the same skill invocation as an activation. The loader writes kind=skill
        # to both tables, so the demo must too, or the skill-side efficiency
        # query reads empty while the scorecard reads full — which looks like a
        # broken query rather than a half-seeded fleet.
        if used_skill:
            si = out["skill_invocation"][-1]
            out["artefact_activation"].append({
                **act,
                "span_id": f"demoact-{n:06d}-k0", "trace_id": si["trace_id"],
                "started_at": si["started_at"], "ended_at": si["ended_at"],
                # when this session is loop-driven, one skill activation is the
                # loop itself — otherwise `self_tokens` is always zero and the
                # split the query exists to show is invisible
                "kind": "skill",
                "name": "demo-epic-loop" if scoped else si["skill_name"], "source": "hook",
                "prompt_id": f"demo-prompt-{n}-0", "parent_prompt_id": None,
                "model": si["model"], "input_tokens": si["input_tokens"],
                "output_tokens": si["output_tokens"],
                "cache_read_tokens": si["cache_read_tokens"],
                "cache_creation_tokens": si["cache_creation_tokens"],
                "llm_requests": si["llm_requests"], "tool_calls": None,
                "duration_ms": None, "is_error": si["is_error"],
                "subagent_type": None, "subagent_id": None, "subagent_depth": None,
                "compaction_reason": None, "compaction_tokens_before": None,
                "compaction_tokens_after": None, "compaction_turns_since_previous": None,
                "hook_ms": None, "hook_ms_by_hook": None,
            })

        # roughly a third of sessions spawn sub-agents, and they are expensive
        for k in range(rng.choice([0, 0, 1, 2, 4])):
            agent, weight = rng.choice(DEMO_SUBAGENTS)
            at = opened + dt.timedelta(minutes=rng.uniform(1, 40))
            sub_in = int(total_in * weight * rng.uniform(0.4, 1.6))
            out["artefact_activation"].append({
                **act,
                "span_id": f"demoact-{n:06d}-s{k}", "trace_id": f"demotrace{n:06d}",
                "started_at": _iso(at), "ended_at": _iso(at + dt.timedelta(seconds=rng.uniform(20, 900))),
                "kind": "subagent", "name": agent, "source": "hook",
                "prompt_id": None, "parent_prompt_id": f"demo-prompt-{n}-0",
                "model": rng.choice(["claude-haiku-4-5-20251001", "claude-opus-5"]),
                "input_tokens": sub_in, "output_tokens": int(sub_in * 0.08),
                # a sub-agent re-reads the parent context on every request, so its
                # cache_read dwarfs everything else — the trap the analyst brief names
                "cache_read_tokens": int(sub_in * rng.uniform(8, 40)),
                "cache_creation_tokens": int(sub_in * rng.uniform(0.1, 0.5)),
                "llm_requests": rng.randint(3, 60), "tool_calls": rng.randint(1, 40),
                "duration_ms": int(rng.uniform(20_000, 900_000)), "is_error": rng.random() < 0.04,
                "subagent_type": agent, "subagent_id": f"demo-agent-{n}-{k}",
                "subagent_depth": 1,
                "compaction_reason": None, "compaction_tokens_before": None,
                "compaction_tokens_after": None, "compaction_turns_since_previous": None,
                "hook_ms": None, "hook_ms_by_hook": None,
            })
        # ADR-010 decision 7: spend outside the harness. Seeded because a panel
        # that only ever shows zero external rows is indistinguishable from one
        # that is broken, and because the *coverage* column needs a mixed picture
        # to be worth reading. Roughly a fifth of these deliberately report no
        # cost: a real emitter's early records had none, and the demo should show
        # what that looks like rather than a tidy 100%.
        for k in range(rng.choice([0, 0, 0, 1, 2])):
            at = opened + dt.timedelta(minutes=rng.uniform(2, 50))
            reported = rng.random() > 0.2
            ext_in = int(total_in * rng.uniform(0.3, 2.5))
            out["artefact_activation"].append({
                **act,
                "span_id": f"demoact-{n:06d}-x{k}", "trace_id": f"demotrace{n:06d}",
                "started_at": _iso(at), "ended_at": _iso(at + dt.timedelta(seconds=rng.uniform(30, 600))),
                "kind": "external", "name": "demo-council", "source": "emitter",
                "prompt_id": None, "parent_prompt_id": None,
                "model": rng.choice(["anthropic/claude-opus-5", "openai/gpt-5.6-sol",
                                     "deepseek/deepseek-v3.2"]),
                "input_tokens": ext_in, "output_tokens": int(ext_in * 0.12),
                "cache_read_tokens": None, "cache_creation_tokens": None,
                "llm_requests": None, "tool_calls": None, "duration_ms": None, "is_error": False,
                "subagent_type": None, "subagent_id": None, "subagent_depth": None,
                "compaction_reason": None, "compaction_tokens_before": None,
                "compaction_tokens_after": None, "compaction_turns_since_previous": None,
                "hook_ms": None, "hook_ms_by_hook": None,
                "external_system": "demo-council",
                "external_operation": rng.choice(["consult", "verify"]),
                # absent, never zero: a zero would be averaged over and read as
                # "this call was free", which is the misreading the contract exists
                # to prevent (ADR-005)
                "external_cost_usd": round(rng.uniform(0.05, 4.5), 4) if reported else None,
                "external_requests": rng.randint(3, 12) if reported else None,
                "external_duration_ms": int(rng.uniform(20_000, 400_000)),
            })
        # ~12% of sessions compact at least once
        if rng.random() < 0.12:
            at = opened + dt.timedelta(minutes=rng.uniform(10, 90))
            before = rng.randint(400_000, 980_000)
            out["artefact_activation"].append({
                **act,
                "span_id": f"demoact-{n:06d}-c0", "trace_id": f"demotrace{n:06d}",
                "started_at": _iso(at), "ended_at": _iso(at),
                "kind": "compaction", "name": "auto", "source": "hook",
                "prompt_id": None, "parent_prompt_id": None, "model": None,
                "input_tokens": None, "output_tokens": None,
                "cache_read_tokens": None, "cache_creation_tokens": None,
                "llm_requests": None, "tool_calls": None, "duration_ms": None, "is_error": False,
                "subagent_type": None, "subagent_id": None, "subagent_depth": None,
                "compaction_reason": "auto", "compaction_tokens_before": before,
                "compaction_tokens_after": int(before * rng.uniform(0.01, 0.05)),
                "compaction_turns_since_previous": turns,
                "hook_ms": None, "hook_ms_by_hook": None,
            })

        if rng.random() < 0.06:
            out["defect"].append({
                "defect_id": f"demo-org/demo-repo#{1000 + n}", "ticket_id": ticket_id,
                "opened_at": _iso(merged + dt.timedelta(days=rng.uniform(1, 28))),
                "severity": rng.choice(["sev2", "sev3"]), "source": "github",
            })
    return out


def columns(rows: list[dict]) -> list[str]:
    """Every column any row supplies, in first-seen order.

    Taking `rows[0]` instead was a silent data loss: the kinds do not share a
    shape — a turn has no `external_system`, an external run has no `hook_ms` —
    so every column the first row happened to lack was dropped for all the
    others. psycopg ignores extra keys in a parameter dict, so nothing raised;
    the external query simply returned one row of NULLs and read as a broken
    query rather than a broken seeder.
    """
    seen: dict = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def fill(rows: list[dict], cols: list[str]) -> list[dict]:
    """Every row gets every column; absent ones are NULL.

    Required once the list is a union: psycopg raises on a *missing* key, and
    NULL is the truthful filler — a turn has no external cost, and that is an
    absence rather than a zero (ADR-005).
    """
    return [{c: row.get(c) for c in cols} for row in rows]


def load(dsn: str, data: dict[str, list[dict]]) -> dict[str, int]:
    import psycopg
    from warehouse.load_delivery import CONFLICT_KEYS, TABLES as DELIVERY_COLS
    conflict = dict(CONFLICT_KEYS, skill_invocation="span_id", session_cost="session_id",
                    artefact_activation="span_id")
    counts = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table, rows in data.items():
            if not rows:
                continue
            cols = columns(rows)
            rows = fill(rows, cols)
            sql = (f"INSERT INTO {table} ({','.join(cols)}) "
                   f"VALUES ({','.join('%(' + c + ')s' for c in cols)}) "
                   f"ON CONFLICT ({conflict[table]}) DO NOTHING")
            cur.executemany(sql, rows)
            counts[table] = len(rows)
    return counts


def clear(dsn: str) -> dict[str, int]:
    """Remove every DEMO- row and nothing else."""
    import psycopg
    counts = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table, column, pattern in (
            ("defect", "defect_id", "demo-org/%"), ("policy_result", "pr_id", "demo-org/%"),
            ("pull_request", "pr_id", "demo-org/%"), ("skill_invocation", "span_id", "demo%"),
            ("session_cost", "session_id", "demo-session-%"), ("ticket", "ticket_id", f"{DEMO_MARKER}-%"),
            ("artefact_activation", "span_id", "demoact-%"),
        ):
            cur.execute(f"DELETE FROM {table} WHERE {column} LIKE %s", (pattern,))
            counts[table] = cur.rowcount
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stdtel-demo", description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default="postgresql://postgres:stdtel@localhost:5432/stdtel")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--prs", type=int, default=120)
    ap.add_argument("--clear", action="store_true", help="remove the demo rows instead of loading")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.clear:
        for table, n in clear(a.dsn).items():
            print(f"  removed {n:>4} from {table}")
        return 0
    for table, n in load(a.dsn, generate(a.seed, a.prs)).items():
        print(f"  loaded {n:>4} into {table}")
    print("\nThis data is synthetic. Every identifier is DEMO-/demo- prefixed; "
          "`make demo-clear` removes it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
