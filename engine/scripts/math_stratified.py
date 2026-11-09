"""Synthetic Monte Carlo evidence for docs/math/02_stratified_guarantees.md.

Run: uv run python scripts/math_stratified.py   (seeded; about 2 minutes)
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from scruple.stats.sequential import BettingEProcess
from scruple.stats.simulate import Ternary, ternary_with_mean
from scruple.stats.stratified import StratifiedMonitor

FREQS = (0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)
SEED = 20261109
STRATA = [f"s{i}" for i in range(len(FREQS))]


def laws_for(regressed: Sequence[int]) -> list[Ternary]:
    return [
        ternary_with_mean(0.2, 0.5, 0.8) if i in regressed else ternary_with_mean(0.0, 0.2, 0.8)
        for i in range(len(FREQS))
    ]


def sample(law: Ternary, u: float) -> float:
    if u < law.p_worse:
        return law.worse
    if u < law.p_worse + law.p_better:
        return -law.better
    return 0.0


def error_rates(regressed: Sequence[int], comonotone: bool, reps: int, n: int, seed: int) -> str:
    alpha = 0.1
    laws = laws_for(regressed)
    nulls = {i for i in range(len(FREQS)) if i not in regressed}
    rng = random.Random(seed)
    fw_bonf = fw_closed = fw_naive = 0
    fdp = 0.0
    hits = 0
    for _ in range(reps):
        mon = StratifiedMonitor(STRATA)
        shared = [rng.random() for _ in range(n)]
        seen = [0] * len(FREQS)
        sums = [0.0] * len(FREQS)
        for t in rng.choices(range(len(FREQS)), weights=FREQS, k=n):
            u = shared[seen[t]] if comonotone else rng.random()
            seen[t] += 1
            x = sample(laws[t], u)
            sums[t] += x
            mon.update(STRATA[t], x)
        bonf = {int(s[1:]) for s in mon.fwer_rejections(alpha)}
        closed = {int(s[1:]) for s in mon.closed_rejections(alpha)}
        ebh = {int(s[1:]) for s in mon.fdr_rejections(alpha)}
        naive = {i for i in range(len(FREQS)) if seen[i] and sums[i] / seen[i] > 0}
        fw_bonf += bool(bonf & nulls)
        fw_closed += bool(closed & nulls)
        fw_naive += bool(naive & nulls)
        fdp += len(ebh & nulls) / max(len(ebh), 1)
        hits += bool(set(regressed) & bonf)
    k0 = len(nulls)
    return (
        f"regressed={tuple(regressed)!s:<12} comonotone={comonotone!s:<5} "
        f"FWER e-Bonf(sup)={fw_bonf / reps:.4f} FWER closed={fw_closed / reps:.4f} "
        f"FDR e-BH={fdp / reps:.4f} (bound {alpha * k0 / len(FREQS):.3f}) "
        f"naive-slice-mean>0 FWER={fw_naive / reps:.3f} "
        f"P(detect>=1 true)={hits / reps:.3f}"
    )


def pooled_vs_stratified(reps: int, n: int, seed: int) -> None:
    alpha = 0.05
    rng = random.Random(seed)
    laws = [ternary_with_mean(-0.01, 0.2, 0.5) for _ in range(len(FREQS) - 1)]
    laws.append(ternary_with_mean(0.35, 0.5, 0.8))
    pooled_mean = sum(f * law.mean for f, law in zip(FREQS, laws, strict=True))
    print(
        f"rare stratum s7 (1% of traffic) mean +0.35, others -0.01; pooled mean {pooled_mean:+.4f}"
    )
    pooled = strat = 0
    for _ in range(reps):
        mon = StratifiedMonitor(STRATA)
        ep = BettingEProcess(0.0, "greater")
        for t in rng.choices(range(len(FREQS)), weights=FREQS, k=n):
            x = laws[t].sample(rng)
            mon.update(STRATA[t], x)
            ep.update(x)
        pooled += ep.rejected(alpha)
        strat += "s7" in mon.fwer_rejections(alpha)
    print(
        f"n={n}, reps={reps}: pooled anytime test flags regression {pooled / reps:.3f}; "
        f"stratified e-Bonferroni flags s7 {strat / reps:.3f}"
    )


if __name__ == "__main__":
    print("## error control, K=8 strata, freqs", FREQS, "alpha=0.1, n=4000, reps=1000")
    for k, regressed in enumerate([(), (0,), (6, 7), (0, 1, 2, 3)]):
        for comonotone in (False, True):
            print(error_rates(regressed, comonotone, 1000, 4000, SEED + 10 * k + comonotone))
    print("\n## pooled vs stratified")
    pooled_vs_stratified(500, 8000, SEED + 99)
