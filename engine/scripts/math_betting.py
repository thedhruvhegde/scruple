"""Variance-aware betting on the E1 planted regressions (docs/math/07_variance_aware_betting.md).

    SCRUPLE_HELDOUT_SALT=... uv run python scripts/math_betting.py collect --jobs 4
    uv run python scripts/math_betting.py analyse

`collect` replays the E1 policies (baseline, R1-R7, C2-C4) on benchmark v1 exactly as
`scripts/exp_planted.py run` does and stores only the paired merchant-cost differences
(candidate minus baseline, USD minor units, sparse) with each case's family and
amount in `results/experiments/E1_paired_costs.json`. No salt, seed or case content is
written. `analyse` reads that file (no salt needed) and compares betting strategies at
equal validity: per-family plus pooled e-processes, weighted e-Bonferroni on running
maxima at alpha 0.05, the E1 detector's gate with cap $200 and shuffle seed 0.
Synthetic episodes only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from scruple.stats.betting import (
    BET_STRATEGIES,
    BetStrategy,
    make_e_process,
    oracle_growth,
    required_cases_lower_bound,
)
from scruple.stats.stratified import e_bonferroni

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "experiments" / "E1_paired_costs.json"
CAP = 20_000  # the E1 detector's cap, USD minor ($200)
ALPHA = 0.05
FOLD = 500


def cmd_collect(args: argparse.Namespace) -> int:
    from scruple.bench.cli import DEFAULT_BENCH
    from scruple.bench.core import BenchFile, cases_for
    from scruple.bench.plants import CONTROLS, REGRESSIONS
    from scruple.bench.runner import Agent, run_agent
    from scruple.bench.spec import space_from

    bench = BenchFile.load(DEFAULT_BENCH)
    cases = cases_for(bench.manifest, space_from(None), args.limit)
    t0 = time.perf_counter()
    base = run_agent(Agent("C1 unmodified", CONTROLS[0]), cases, jobs=args.jobs)
    rows = [r for b in base for r in b.rows]
    print(f"baseline: {len(rows)} cases in {time.perf_counter() - t0:.0f}s", flush=True)
    out: dict[str, Any] = {
        "experiment": "E1 planted regressions, paired merchant costs",
        "synthetic": True,
        "benchmark": {"version": bench.manifest.version, "manifest_hash": bench.manifest_hash},
        "units": "USD minor",
        "n_cases": len(rows),
        "family": [r.family for r in rows],
        "amount_minor": [r.amount_minor for r in rows],
        "policies": {},
    }
    for p in [*REGRESSIONS, *CONTROLS[1:]]:
        t = time.perf_counter()
        res = run_agent(Agent(p.name, p), cases, jobs=args.jobs)
        cand = [r for b in res for r in b.rows]
        diffs = {
            str(i): c.cost - b.cost
            for i, (b, c) in enumerate(zip(rows, cand, strict=True))
            if c.cost != b.cost
        }
        out["policies"][p.name] = diffs
        print(f"{p.name}: {len(diffs)} non-zero diffs ({time.perf_counter() - t:.0f}s)", flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, sort_keys=True) + "\n")
    print(f"written to {args.out}")
    return 0


def _diffs(data: dict[str, Any], name: str) -> list[int]:
    sparse = data["policies"][name]
    return [int(sparse.get(str(i), 0)) for i in range(data["n_cases"])]


def gate_flags(
    diffs: Sequence[int],
    families: Sequence[str],
    strategy: BetStrategy,
    *,
    cap: float = CAP,
    alpha: float = ALPHA,
    shuffle_seed: int | None = 0,
) -> tuple[str, ...]:
    """The E1 gate's block family with a chosen betting strategy (same validity)."""
    order = list(range(len(diffs)))
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(order)
    types = sorted(set(families))
    procs = {t: make_e_process(strategy, 0.0, "greater") for t in types}
    pooled = make_e_process(strategy, 0.0, "greater")
    for i in order:
        x = max(-1.0, min(1.0, diffs[i] / cap))
        procs[families[i]].update(x)
        pooled.update(x)
    names = [*types, "__pooled__"]
    maxima = [procs[t].max_e_value for t in types] + [pooled.max_e_value]
    return tuple(names[i] for i in e_bonferroni(maxima, alpha))


def best_log_e(diffs: Sequence[int], families: Sequence[str], strategy: BetStrategy) -> float:
    """Largest running-max log e-value over the families and the pooled process."""
    order = list(range(len(diffs)))
    random.Random(0).shuffle(order)
    procs = {t: make_e_process(strategy, 0.0, "greater") for t in sorted(set(families))}
    pooled = make_e_process(strategy, 0.0, "greater")
    for i in order:
        x = max(-1.0, min(1.0, diffs[i] / CAP))
        procs[families[i]].update(x)
        pooled.update(x)
    return max(math.log(p.max_e_value) for p in [*procs.values(), pooled])


def null_type_one(
    strategy: BetStrategy, diffs: Sequence[int], *, reps: int, seed: int, n: int
) -> float:
    """Rejection rate on i.i.d. resamples of the centred differences (a boundary null)."""
    xs = [d / CAP for d in diffs]
    mu = math.fsum(xs) / len(xs)
    centred = [x - mu for x in xs if -1.0 <= x - mu <= 1.0]
    rng = random.Random(seed)
    hits = 0
    for _ in range(reps):
        ep = make_e_process(strategy, 0.0, "greater")
        for _ in range(n):
            ep.update(rng.choice(centred))
        hits += ep.rejected(ALPHA)
    return hits / reps


def cmd_analyse(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.data).read_text())
    fam: list[str] = data["family"]
    n = data["n_cases"]
    print(f"## E1 paired merchant costs, benchmark v1, n={n}, cap {CAP} USD minor (synthetic)")
    print(f"strategies: {', '.join(BET_STRATEGIES)}; alpha {ALPHA}, 6 families + pooled\n")
    names = list(data["policies"])
    summary: dict[str, Any] = {}
    for name in names:
        d = _diffs(data, name)
        mean = math.fsum(d) / n
        var = math.fsum((x - mean) ** 2 for x in d) / (n - 1)
        t_stat = mean / math.sqrt(var / n) if var > 0 else 0.0
        full = {s: gate_flags(d, fam, s) for s in BET_STRATEGIES}
        folds: dict[str, int] = {}
        exercised = 0
        for s in BET_STRATEGIES:
            folds[s] = 0
        for f in range(n // FOLD):
            sl = slice(f * FOLD, (f + 1) * FOLD)
            dd, ff = d[sl], fam[sl]
            if any(dd):
                exercised += 1
            for s in BET_STRATEGIES:
                folds[s] += bool(gate_flags(dd, ff, s))
        print(
            f"{name}: mean diff {mean:.2f} USD minor/case (sd {math.sqrt(var):.1f}, t {t_stat:.2f})"
        )
        for s in BET_STRATEGIES:
            print(
                f"    {s:<10} full: {list(full[s]) or 'hold'}; "
                f"folds with a gate block {folds[s]}/20"
            )
        summary[name] = {
            "mean": mean,
            "sd": math.sqrt(var),
            "t": t_stat,
            "full": {s: list(v) for s, v in full.items()},
            "folds": folds,
            "cost_changed_folds": exercised,
        }

    print("\n## best gate e-value (max over the 6 families and pooled, running max), log scale")
    print(f"rejection needs log E >= log(7/{ALPHA}) = {math.log(7 / ALPHA):.2f}")
    for name in names:
        d = _diffs(data, name)
        if not any(d):
            continue
        row = []
        for s_ in BET_STRATEGIES:
            row.append(f"{s_} {best_log_e(d, fam, s_):.2f}")
        print(f"{name}: " + ", ".join(row))
        summary[name]["best_log_e"] = {s_: best_log_e(d, fam, s_) for s_ in BET_STRATEGIES}

    print("\n## power by i.i.d. resampling of cases (family kept with its diff)")
    print(f"P(gate blocks) over {args.power_reps} resamples per cell, seed 7")
    regs = [k for k in names if k.startswith("R") and any(_diffs(data, k))]
    power: dict[str, Any] = {}
    for name in regs:
        d = _diffs(data, name)
        for m in (2_500, 5_000, 10_000):
            cells = []
            for s_ in BET_STRATEGIES:
                rng = random.Random(7)
                hits = 0
                for _ in range(args.power_reps):
                    idx = [rng.randrange(n) for _ in range(m)]
                    hits += bool(
                        gate_flags(
                            [d[i] for i in idx], [fam[i] for i in idx], s_, shuffle_seed=None
                        )
                    )
                cells.append(f"{s_} {hits / args.power_reps:.2f}")
                power.setdefault(name, {}).setdefault(str(m), {})[s_] = hits / args.power_reps
            print(f"{name} n={m:>6}: " + ", ".join(cells))

    print("\n## the floor for any valid test (Prop 7.2) on the observed paired costs")
    print("g* = max_lambda mean log(1 + lambda x), x = clip(diff / cap); per family and pooled")
    print("n_k: cases of that family any valid level-alpha test needs for power 0.8;")
    print("total = n_k / share of the family")
    floor: dict[str, Any] = {}
    for name in regs:
        d = _diffs(data, name)
        by: dict[str, list[float]] = defaultdict(list)
        for x, k in zip(d, fam, strict=True):
            by[k].append(max(-1.0, min(1.0, x / CAP)))
        by["__pooled__"] = [max(-1.0, min(1.0, x / CAP)) for x in d]
        print(f"{name}:")
        for k, xs in sorted(by.items()):
            mean_k = math.fsum(xs) / len(xs) * CAP
            g, lam = oracle_growth(xs, 0.0)
            share = len(xs) / n
            if g <= 0:
                print(f"    {k:<20} mean {mean_k:8.2f}: no positive growth (mean not above 0)")
                continue
            n1 = required_cases_lower_bound(g, ALPHA, 0.2)
            n7 = required_cases_lower_bound(g, ALPHA / 7, 0.2)
            print(
                f"    {k:<20} mean {mean_k:8.2f} g*={g:.3e} lambda*={lam:.3f}: "
                f"n_k >= {n1:,.0f} (alpha 0.05), {n7:,.0f} (alpha 0.05/7); "
                f"total >= {n1 / share:,.0f} / {n7 / share:,.0f}"
            )
            floor.setdefault(name, {})[k] = {
                "mean": mean_k,
                "g": g,
                "lam": lam,
                "n_k": n1,
                "n_k7": n7,
                "total": n1 / share,
                "total7": n7 / share,
            }

    print("\n## validity at the null boundary: i.i.d. resamples of centred diffs, one e-process")
    for label in ("R5 concede everything under $25", "R3 wrong documents cited on visa 13.1"):
        dd = _diffs(data, label)
        for s_ in BET_STRATEGIES:
            rate = null_type_one(s_, dd, reps=args.reps, seed=11, n=args.null_n)
            se = math.sqrt(ALPHA * (1 - ALPHA) / args.reps)
            print(
                f"    {label[:2]} {s_:<11} P(reject by n={args.null_n}) = {rate:.4f} "
                f"(alpha {ALPHA}, 3 SE {3 * se:.4f})"
            )
    summary["__power__"] = power
    summary["__floor__"] = floor
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.set_defaults(fn=cmd_collect)
    c.add_argument("--jobs", type=int, default=4)
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--out", default=str(OUT))
    a = sub.add_parser("analyse")
    a.set_defaults(fn=cmd_analyse)
    a.add_argument("--data", default=str(OUT))
    a.add_argument("--reps", type=int, default=400)
    a.add_argument("--null-n", type=int, default=2000)
    a.add_argument("--power-reps", type=int, default=100)
    a.add_argument("--json", default=None)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
