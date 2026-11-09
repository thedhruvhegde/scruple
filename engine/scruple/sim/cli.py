"""`scruple sim ...`: simulator utilities (planning, scenarios, fidelity)."""

from __future__ import annotations

import argparse
from typing import Any


def cmd_heldout_size(args: argparse.Namespace) -> int:
    from scruple.sim.difficulty import difficulty_mix
    from scruple.sim.disputes import generate_cases
    from scruple.sim.heldout import check_heldout, heldout_size, mix_shares

    mix = difficulty_mix(args.difficulty)
    kw: dict[str, Any] = {
        "cost_cap": args.cap,
        "min_detectable_regression": args.regression,
        "sd": args.sd,
        "alpha": args.alpha,
        "beta": 1 - args.power,
        "allocation": args.allocation,
    }
    if args.check is not None:
        res = check_heldout(generate_cases(args.seed, args.check, mix), **kw)
    else:
        res = heldout_size(mix_shares(mix), **kw)
    print(f"synthetic mix at difficulty {args.difficulty} (money in USD minor units)")
    print("\n".join(res.lines()))
    return 0


def add_sim_parser(sub: Any) -> None:
    p = sub.add_parser("sim", help="dispute simulator utilities")
    ss = p.add_subparsers(dest="sim_cmd", required=True)
    h = ss.add_parser("heldout-size", help="cases per type a held-out set needs (scruple.stats)")
    h.set_defaults(fn=cmd_heldout_size)
    h.add_argument("--difficulty", type=float, default=0.5, help="0..1 (0.5 = FULL_MIX)")
    h.add_argument("--cap", type=float, default=10_000, help="per-case cost cap, USD minor")
    h.add_argument("--regression", type=float, default=500, help="regression to detect")
    h.add_argument("--sd", type=float, default=2_000, help="per-case sd of the loss change")
    h.add_argument("--alpha", type=float, default=0.05)
    h.add_argument("--power", type=float, default=0.8)
    h.add_argument("--allocation", choices=["uniform", "optimised"], default="uniform")
    h.add_argument("--check", type=int, default=None, help="generate N cases and check them")
    h.add_argument("--seed", type=int, default=1, help="seed for --check")
    sc = ss.add_parser("scenarios", help="run the hand-written scenarios through the engine")
    sc.set_defaults(fn=cmd_scenarios)
    sc.add_argument("--dir", default=None, help="scenario directory (default: built-in)")


def cmd_scenarios(args: argparse.Namespace) -> int:
    from scruple.sim.scenario import SCENARIO_DIR, load_scenarios, run_scenario

    runs = [run_scenario(s) for s in load_scenarios(args.dir or SCENARIO_DIR)]
    for r in runs:
        print(f"{'ok  ' if r.passed else 'FAIL'} {r.scenario_id}")
        for p in r.problems:
            print(f"     {p}")
    failed = sum(1 for r in runs if not r.passed)
    print(f"{len(runs) - failed}/{len(runs)} scenarios passed (synthetic)")
    return 1 if failed else 0
