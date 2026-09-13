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
import random
import sys
from pathlib import Path

DEMO_MARKER = "DEMO"
TABLES = ("ticket", "pull_request", "policy_result", "skill_invocation", "session_cost", "defect")

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

        if rng.random() < 0.06:
            out["defect"].append({
                "defect_id": f"demo-org/demo-repo#{1000 + n}", "ticket_id": ticket_id,
                "opened_at": _iso(merged + dt.timedelta(days=rng.uniform(1, 28))),
                "severity": rng.choice(["sev2", "sev3"]), "source": "github",
            })
    return out


def load(dsn: str, data: dict[str, list[dict]]) -> dict[str, int]:
    import psycopg
    from warehouse.load_delivery import CONFLICT_KEYS, TABLES as DELIVERY_COLS
    conflict = dict(CONFLICT_KEYS, skill_invocation="span_id", session_cost="session_id")
    counts = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table, rows in data.items():
            if not rows:
                continue
            cols = list(rows[0])
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
