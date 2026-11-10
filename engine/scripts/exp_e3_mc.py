"""Seeded Monte Carlo for docs/math/07_variance_reduction.md (synthetic, deterministic).

    uv run python scripts/exp_e3_mc.py            # full tables (several minutes)
    uv run python scripts/exp_e3_mc.py --quick    # small version

Type-I error under nulls with optional stopping (every rate is "ever rejected by n",
the supremum over stopping rules), and power of the variance-reduced tests against
the current ones. Every number is synthetic.
"""

from __future__ import annotations

import argparse
import math
import random
import sys

from scruple.stats.planning import StratumSpec, stratum_cases_needed
from scruple.stats.sequential import BettingEProcess
from scruple.stats.simulate import (
    RareShock,
    Sampler,
    ScaledBeta,
    Ternary,
    binomial_se,
    ternary_with_mean,
)
from scruple.stats.variance import (
    ControlVariateEProcess,
    StratumLaw,
    block_rates,
    certificate_rates,
    cv_cases_needed,
    stratified_cases_needed,
)

ALPHA = 0.05
SHARES = (0.28, 0.22, 0.20, 0.18, 0.06, 0.06)


def strata(means: list[float], p: float = 0.2, mag: float = 0.5) -> list[StratumLaw]:
    return [
        StratumLaw(f"s{i}", s, ternary_with_mean(m, p, mag))
        for i, (s, m) in enumerate(zip(SHARES, means, strict=True))
    ]


def shapes() -> list[StratumLaw]:
    laws: list[Sampler] = [
        Ternary(0.05, 0.6, 0.05, 0.6),
        ScaledBeta(2.0, 2.0),
        RareShock(0.01, 0.0),
        Ternary(0.3, 0.1, 0.1, 0.3),
        ScaledBeta(0.5, 0.5),
        Ternary(0.0, 0.0, 0.0, 0.0),
    ]
    return [
        StratumLaw(f"s{i}", s, law) for i, (s, law) in enumerate(zip(SHARES, laws, strict=True))
    ]


def fmt(d: dict[str, float]) -> str:
    return ", ".join(f"{k}={v:.3f}" for k, v in d.items())


def type_one(reps: int, n: int) -> None:
    print(f"## block tests: type-I error, sup over time (alpha={ALPHA}, reps={reps}, n={n})")
    print(f"3 x binomial SE at alpha: {3 * binomial_se(ALPHA, reps):.4f}")
    nulls = {
        "boundary (all types mean 0)": strata([0.0] * 6),
        "mixed (some types below 0)": strata([0.0, -0.02, -0.05, 0.0, -0.01, 0.0]),
        "shapes (6 laws, mean 0)": shapes(),
    }
    for name, law in nulls.items():
        r = block_rates(law, n, reps, seed=101, checkpoints=(n // 4, n // 2))
        print(f"{name}: {fmt(r)}")
    print()


def power(reps: int, n: int) -> None:
    print(f"## block tests: power P(block by n={n}), reps={reps}")
    print("types with shares (0.28, 0.22, 0.20, 0.18, 0.06, 0.06); X ternary, P(X != 0) = 0.2")
    scen = {
        "one 6% type up by d, others down 0.01": lambda d: [-0.01] * 5 + [d],
        "one 6% type up by d, others at 0": lambda d: [0.0] * 5 + [d],
        "two types up by d (28%, 18%)": lambda d: [d, 0.0, 0.0, d, 0.0, 0.0],
        "every type up by d/5": lambda d: [d / 5] * 6,
    }
    for name, f in scen.items():
        print(f"{name}:")
        for d in (0.02, 0.04, 0.06, 0.08, 0.1):
            r = block_rates(strata(f(d)), n, reps, seed=202)
            print(f"  d={d:.2f}: {fmt(r)}")
    print()


def hetero(offset: float) -> list[StratumLaw]:
    means = [0.3, -0.3, 0.2, -0.2, 0.1, -0.1]
    return [
        StratumLaw(f"s{i}", s, ternary_with_mean(m + offset, 0.6, 0.6))
        for i, (s, m) in enumerate(zip(SHARES, means, strict=True))
    ]


def certificates(reps: int, n: int) -> None:
    print(f"## certificates (marginal null, known type shares), reps={reps}, n={n}")
    print("type means (0.3, -0.3, 0.2, -0.2, 0.1, -0.1) + offset; null: marginal mean >= m0")
    base = hetero(0.0)
    m0 = math.fsum(s.share * s.law.mean for s in base)
    print(
        f"null boundary (offset 0, m0 = {m0:.4f}): {fmt(certificate_rates(base, m0, n, reps, 303))}"
    )
    for off in (0.01, 0.02, 0.03, 0.04):
        r = certificate_rates(hetero(-off), m0, n, reps, 304)
        print(f"  improvement {off:.2f}: {fmt(r)}")
    print()


def cv_heuristic(reps: int, n: int) -> None:
    print(f"## control variate, one covariate: P(reject by n={n}), reps={reps}, mean 0.02")
    print("X = 0.02 + b Z + U(-0.2, 0.2), Z ~ U(-1, 1); known centre vs running centre")
    rng = random.Random(404)
    for b in (0.0, 0.2, 0.4, 0.6):
        hits = {"plain": 0, "known": 0, "running": 0}
        for _ in range(reps):
            plain = BettingEProcess(0.0, "greater")
            known = ControlVariateEProcess(0.0, "greater", 1, covariate_mean=[0.0])
            running = ControlVariateEProcess(0.0, "greater", 1, centre="running")
            for _ in range(n):
                z = rng.uniform(-1, 1)
                x = max(-1.0, min(1.0, 0.02 + b * z + rng.uniform(-0.2, 0.2)))
                plain.update(x)
                known.update(x, [z])
                running.update(x, [z])
            hits["plain"] += plain.rejected(ALPHA)
            hits["known"] += known.rejected(ALPHA)
            hits["running"] += running.rejected(ALPHA)
        rho = b / math.sqrt(b * b + 0.04)  # Var(bZ) = b^2/3, Var(U) = 0.04/3
        print(f"  b={b:.1f} (corr {rho:.2f}): " + fmt({k: v / reps for k, v in hits.items()}))
    print()


def planning() -> None:
    print("## planning: cases of one 6% type to flag its regression (bernstein, power 0.8)")
    spec = StratumSpec(
        name="rare", frequency=0.06, cost_cap=20_000, min_detectable_regression=1_000, sd=4_000
    )
    k = 6
    cur = stratum_cases_needed(spec, ALPHA / (k + 1), 0.2)[0]
    strat = stratified_cases_needed(spec, ALPHA, 0.2, share_of_alpha=0.8)[0]
    print("cap 20000, regression 1000, sd 4000 (USD minor, illustrative)")
    print(f"  current (e-Bonferroni, alpha/(K+1), K=6): {cur}")
    print(f"  stratified (0.8 alpha):                    {strat}")
    for r2 in (0.0, 0.25, 0.5, 0.75):
        n_cv = cv_cases_needed(spec, ALPHA, 0.2, r2=r2)[0]
        print(f"  control variate, R^2={r2:.2f}, theta_max=1:  {n_cv}")
    print(
        f"  plain at alpha (reference):                {stratum_cases_needed(spec, ALPHA, 0.2)[0]}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args(argv)
    if a.quick:
        type_one(200, 800)
        power(100, 1500)
        certificates(100, 1000)
        cv_heuristic(50, 1500)
    else:
        type_one(1000, 3000)
        power(300, 2000)
        certificates(300, 2000)
        cv_heuristic(200, 2000)
    planning()
    return 0


if __name__ == "__main__":
    sys.exit(main())
