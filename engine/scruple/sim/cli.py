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
    f = ss.add_parser("fidelity", help="compare the synthetic mix with real CFPB marginals")
    f.set_defaults(fn=cmd_fidelity)
    f.add_argument("--profile", choices=["default", "calibrated"], default="default")
    f.add_argument("--n", type=int, default=4_000, help="synthetic cases")
    f.add_argument("--seed", type=int, default=1)
    f.add_argument("--boot", type=int, default=2_000, help="bootstrap resamples")
    f.add_argument("--calibrate", action="store_true", help="fit family weights; before/after")
    f.add_argument("--calibration-n", type=int, default=1_500, help="cases per fit evaluation")
    f.add_argument("--save", action="store_true", help="with --calibrate: write the profile")
    f.add_argument("--raw-dir", default=None, help="rebuild counts from the raw CFPB CSVs")
    f.add_argument("--write-aggregates", action="store_true", help="with --raw-dir: save them")


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


def cmd_fidelity(args: argparse.Namespace) -> int:
    import json

    from scruple.sim.disputes import FULL_MIX
    from scruple.sim.fidelity import (
        AGGREGATES,
        CALIBRATED,
        build_aggregates,
        calibrate,
        calibrated_mix,
        fidelity,
        load_aggregates,
        save_calibration,
    )

    if args.raw_dir:
        agg = build_aggregates(args.raw_dir)
        if args.write_aggregates:
            AGGREGATES.write_text(json.dumps(agg, indent=1, sort_keys=True) + "\n")
            print(f"wrote {AGGREGATES}")
    else:
        agg = load_aggregates()
    for p in agg["provenance"]:
        print(
            f"real data: {p['id']} {p['mirror_repo']}@{p['mirror_commit'][:12]} "
            f"sha256 {p['file_sha256'][:16]} ({p['licence']})"
        )
    n, seed, boot = args.n, args.seed, args.boot
    if args.calibrate:
        before = fidelity(FULL_MIX, "FULL_MIX (default)", agg, n, seed, boot)
        cal = calibrate(agg, n=args.calibration_n)
        after_mix = FULL_MIX.model_copy(update={"family_weights": cal.family_weights})
        after = fidelity(after_mix, "calibrated", agg, n, seed, boot)
        print("\n".join(before.lines()))
        print("\n".join(after.lines()))
        print(
            f"fitted: fraud share {cal.fraud_share:.4f}, valid share within fraud "
            f"{cal.valid_fraud:.4f}, within other families {cal.valid_other:.4f}"
        )
        if args.save:
            save_calibration(cal)
            print(f"wrote {CALIBRATED}")
        return 0
    mix = calibrated_mix() if args.profile == "calibrated" else FULL_MIX
    print("\n".join(fidelity(mix, args.profile, agg, n, seed, boot).lines()))
    return 0
