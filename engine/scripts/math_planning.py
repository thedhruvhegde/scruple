"""Synthetic evidence for docs/math/03_sample_size.md.

Run: uv run python scripts/math_planning.py   (seeded; about 1 minute)
"""

from __future__ import annotations

import random

from scruple.stats.planning import (
    StratumSpec,
    plan,
    simulate_power,
    stratum_cases_needed,
)
from scruple.stats.sequential import BettingEProcess
from scruple.stats.simulate import ternary_with_moments

SEED = 20261109
FREQS = (0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)
# Illustrative (ASSUMPTION, synthetic): cap $100 per case, detect a $5 mean regression,
# per-case sd of the paired difference $20, tolerance $0.
SPECS = [
    StratumSpec(name=f"s{i}", frequency=f, cost_cap=10_000, min_detectable_regression=500, sd=2000)
    for i, f in enumerate(FREQS)
]


def single_stratum() -> None:
    print("## one stratum: cap $100, MDE $5, sd $20, alpha_k = 0.05/8, beta = 0.2")
    s = SPECS[-1]
    a_k = 0.05 / 8
    for method in ("hoeffding", "bernstein", "clt"):
        n, bet = stratum_cases_needed(s, a_k, 0.2, method)  # type: ignore[arg-type]
        fixed = simulate_power(s, n, a_k, bet=bet, reps=1000, seed=SEED)
        adaptive = simulate_power(s, n, a_k, bet=None, reps=1000, seed=SEED)
        print(
            f"{method:<9}: n_k*={n:>5}, bet={bet:.3f}, simulated power fixed-bet={fixed:.3f}, "
            f"aGRAPA={adaptive:.3f}"
        )
    lo, hi = 1, stratum_cases_needed(s, a_k, 0.2, "bernstein")[0]
    _, bet = stratum_cases_needed(s, a_k, 0.2, "clt")
    while hi - lo > 5:  # smallest n with simulated power >= 0.8 at the CLT bet
        mid = (lo + hi) // 2
        if simulate_power(s, mid, a_k, bet=bet, reps=1000, seed=SEED + 1) >= 0.8:
            hi = mid
        else:
            lo = mid
    print(f"simulated minimal n (fixed CLT bet, 1000 reps, +-5): about {hi}")


def full_plans() -> None:
    print("\n## eight strata", FREQS, "alpha=0.05 FWER, beta=0.2, gamma=0.05")
    for method in ("bernstein", "clt"):
        for allocation in ("uniform", "optimised"):
            p = plan(
                SPECS,
                method=method,  # type: ignore[arg-type]
                allocation=allocation,  # type: ignore[arg-type]
                daily_cases=2000,
                canary_fraction=0.1,
            )
            rare = p.strata[-1]
            print(
                f"{method:<9} {allocation:<9}: total cases N={p.total_cases:>6} "
                f"(binding {p.binding_stratum}), canary days at 2000/day x 10% = {p.days:.1f}; "
                f"rare stratum alpha_k={rare.alpha:.4f}, n_k*={rare.stratum_cases}"
            )


def validate_plan(reps: int) -> None:
    p = plan(SPECS, method="bernstein", allocation="optimised")
    print(f"\n## validate optimised bernstein plan by simulation: N={p.total_cases}, reps={reps}")
    rng = random.Random(SEED + 7)
    laws = [ternary_with_moments(s.gap, s.sigma) for s in SPECS]
    hits = [0] * len(SPECS)
    null_fw = 0
    for _ in range(reps):
        procs = [BettingEProcess(0.0, "greater", "fixed", fixed_bet=sp.bet) for sp in p.strata]
        nulls = [BettingEProcess(0.0, "greater", "fixed", fixed_bet=sp.bet) for sp in p.strata]
        null_law = ternary_with_moments(0.0, 0.2)
        for t in rng.choices(range(len(SPECS)), weights=FREQS, k=p.total_cases):
            procs[t].update(laws[t].sample(rng))
            nulls[t].update(null_law.sample(rng))
        for k, sp in enumerate(p.strata):
            hits[k] += procs[k].rejected(sp.alpha)
        null_fw += any(nulls[k].rejected(sp.alpha) for k, sp in enumerate(p.strata))
    target = 0.8 * 0.95
    print(
        "per-stratum power at the MDE: "
        + ", ".join(f"{sp.name}={h / reps:.3f}" for sp, h in zip(p.strata, hits, strict=True))
        + f"  (guarantee >= {target:.2f})"
    )
    print(f"FWER with every stratum at the null boundary: {null_fw / reps:.4f} (alpha 0.05)")


if __name__ == "__main__":
    single_stratum()
    full_plans()
    validate_plan(200)
