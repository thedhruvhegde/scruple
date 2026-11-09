"""Synthetic Monte Carlo evidence for docs/math/01_anytime_paired_test.md.

Run: uv run python scripts/math_sequential.py   (seeded; about 2 minutes)
"""

from __future__ import annotations

import random

from scruple.stats.sequential import BettingConfidenceSequence, sequential_test
from scruple.stats.simulate import (
    RareShock,
    Sampler,
    ScaledBeta,
    binomial_se,
    draw,
    peeking_z_rejects,
    stopped_rejections,
    stopping_rules,
    ternary_with_mean,
)

ALPHA = 0.05
REPS = 2000
HORIZON = 2000
SEED = 20261109

NULLS: dict[str, Sampler] = {
    "ternary_mostly_zero": ternary_with_mean(0.0, 0.1, 0.8),
    "continuous_beta": ScaledBeta(2.0, 2.0),
    "rare_large_loss": RareShock(0.02, 0.0),
    "always_changes": ternary_with_mean(0.0, 1.0, 1.0),
}


def type_one() -> None:
    print(f"## type-I error at the null boundary (alpha={ALPHA}, reps={REPS}, horizon={HORIZON})")
    print(f"3 x binomial SE at alpha: {3 * binomial_se(ALPHA, REPS):.4f}")
    worst = 0.0
    for k, (name, dist) in enumerate(NULLS.items()):
        rng = random.Random(SEED + k)
        rules = stopping_rules(ALPHA, HORIZON, SEED + k)
        counts: dict[str, int] = {}
        naive = 0
        for _ in range(REPS):
            xs = draw(dist, HORIZON, rng)
            for rule, rej in stopped_rejections(xs, 0.0, "greater", ALPHA, rules).items():
                counts[rule] = counts.get(rule, 0) + rej
            naive += peeking_z_rejects(xs, 0.0, ALPHA, every=10)
        row = ", ".join(f"{r}={c / REPS:.4f}" for r, c in sorted(counts.items()))
        worst = max(worst, max(c / REPS for c in counts.values()))
        print(f"{name}: {row}; naive_z_peek_every_10={naive / REPS:.4f}")
    print(f"worst betting rejection rate over all nulls and rules: {worst:.4f}")


def power() -> None:
    reps, horizon = 1000, 3000
    print(f"\n## power: P(reject by n={horizon}), ternary p_changed=0.1 |x|=0.8, reps={reps}")
    rng = random.Random(SEED + 100)
    for mu in (0.0, 0.0025, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04):
        dist = ternary_with_mean(mu, 0.1, 0.8)
        stops: list[int] = []
        hits = 0
        for _ in range(reps):
            res = sequential_test(draw(dist, horizon, rng), 0.0, "greater", ALPHA)
            hits += res.rejected
            if res.rejected:
                stops.append(res.stopped_at)
        med = sorted(stops)[len(stops) // 2] if stops else None
        print(f"mu={mu:.4f}: power={hits / reps:.3f}, median stopping n={med}")


def coverage() -> None:
    reps, horizon = 500, 2000
    print(f"\n## confidence sequence (alpha=0.1), mu=0.01, reps={reps}")
    rng = random.Random(SEED + 200)
    dist = ternary_with_mean(0.01, 0.2, 0.7)
    checkpoints = (25, 100, 500, 1000, 2000)
    covered = 0
    widths: dict[int, float] = dict.fromkeys(checkpoints, 0.0)
    for _ in range(reps):
        cs = BettingConfidenceSequence(0.1)
        ok = True
        for t, x in enumerate(draw(dist, horizon, rng), start=1):
            cs.update(x)
            if t in widths:
                lo, hi = cs.nested_interval()
                widths[t] += (hi - lo) / reps
                ok = ok and lo <= dist.mean <= hi
        covered += ok
    print(f"simultaneous coverage at checkpoints {checkpoints}: {covered / reps:.3f}")
    print("mean width: " + ", ".join(f"n={t}: {w:.4f}" for t, w in widths.items()))


if __name__ == "__main__":
    type_one()
    power()
    coverage()
