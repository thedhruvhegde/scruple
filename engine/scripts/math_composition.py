"""Synthetic evidence for docs/math/08_composition.md (seeded, about a minute).

Run: uv run python scripts/math_composition.py
"""

from __future__ import annotations

import math
import random

from scruple.stats.composition import (
    certify_every_type,
    false_block_bound,
    spending_levels,
)
from scruple.stats.sequential import BettingEProcess
from scruple.stats.stratified import e_bonferroni

SEED = 20261110
FREQS = (0.30, 0.20, 0.15, 0.12, 0.10, 0.08, 0.04, 0.01)
ALPHA = 0.05


def draw(mu: float, rng: random.Random) -> float:
    """Bounded noise around ``mu``: ``mu + U(-0.5, 0.5)`` (needs ``|mu| <= 0.5``)."""
    return mu + rng.uniform(-0.5, 0.5)


def stream(means: tuple[float, ...], n: int, rng: random.Random) -> list[tuple[str, float]]:
    out = []
    for _ in range(n):
        u, acc, k = rng.random(), 0.0, len(FREQS) - 1
        for j, f in enumerate(FREQS):
            acc += f
            if u < acc:
                k = j
                break
        out.append((f"s{k}", draw(means[k], rng)))
    return out


def pooled_certified(xs: list[tuple[str, float]], alpha: float) -> bool:
    ep = BettingEProcess(0.0, "less")
    for _, x in xs:
        ep.update(x)
    return ep.rejected(alpha)


def rare_type(reps: int) -> None:
    tol = {f"s{k}": 0.0 for k in range(len(FREQS))}
    cases = {
        "rare type harmful (+0.35), others -0.01": (-0.01,) * 7 + (0.35,),
        "one common type at the boundary (0), others -0.05": (0.0,) + (-0.05,) * 7,
        "every type improves by 0.05": (-0.05,) * 8,
        "every type improves by 0.15": (-0.15,) * 8,
    }
    print(f"## promote-side guarantee, 8 types {FREQS}, alpha {ALPHA}, reps {reps}")
    print("pooled: certificate on the pooled mean only; per-type: promote iff every type certified")
    for label, means in cases.items():
        pooled_mean = math.fsum(f * m for f, m in zip(FREQS, means, strict=True))
        for n in (2_000, 8_000, 20_000):
            rng = random.Random(SEED + n)
            pc = tc = 0
            for _ in range(reps):
                xs = stream(means, n, rng)
                pc += pooled_certified(xs, ALPHA)
                tc += certify_every_type(xs, tol, ALPHA).all_certified
            print(
                f"{label} (pooled mean {pooled_mean:+.4f}), n={n:>6}: "
                f"P(promote) pooled={pc / reps:.3f}, per-type={tc / reps:.3f}"
            )


def false_blocks(reps: int) -> None:
    phi = 19 / 10_000
    print(
        f"\n## block-side: honest candidate, hard-check false-alarm phi={phi} per case, reps {reps}"
    )
    print("statistical block family at the null boundary (every type mean 0), alpha_block 0.05")
    for n in (200, 500, 2_000, 10_000):
        rng = random.Random(SEED + 7 * n)
        stat = hard = either = 0
        for _ in range(reps):
            procs = {f"s{k}": BettingEProcess(0.0, "greater") for k in range(len(FREQS))}
            flag = False
            for k, x in stream((0.0,) * 8, n, rng):
                procs[k].update(x)
                flag = flag or rng.random() < phi
            s = bool(e_bonferroni([p.max_e_value for p in procs.values()], ALPHA))
            stat += s
            hard += flag
            either += s or flag
        print(
            f"n={n:>6}: P(block) stat={stat / reps:.3f} hard={hard / reps:.3f} "
            f"either={either / reps:.3f}; bound {false_block_bound(ALPHA, [phi] * n):.3f}"
        )


def resubmission(reps: int, k: int, n: int) -> None:
    levels = spending_levels(ALPHA, k)
    print(f"\n## resubmission: {k} harmful versions at the boundary (mean = tolerance = 0)")
    print(
        f"each on {n} fresh cases, mean + U(-0.9, 0.9); reps {reps}; spending sum {sum(levels):.4f}"
    )
    rng = random.Random(SEED + 99)
    fixed_any = [0] * k
    spend_any = [0] * k
    for _ in range(reps):
        hit_f = hit_s = False
        for j in range(k):
            ep = BettingEProcess(0.0, "less")
            for _ in range(n):
                ep.update(rng.uniform(-0.9, 0.9))
            hit_f = hit_f or ep.rejected(ALPHA)
            hit_s = hit_s or ep.rejected(levels[j])
            fixed_any[j] += hit_f
            spend_any[j] += hit_s
    for j in (0, 4, 9, k - 1):
        print(
            f"after {j + 1:>2} submissions: P(some harmful version promoted) "
            f"fixed alpha={fixed_any[j] / reps:.3f} (bound {min(1.0, (j + 1) * ALPHA):.2f}), "
            f"spending={spend_any[j] / reps:.3f} (bound {ALPHA})"
        )


if __name__ == "__main__":
    rare_type(reps=100)
    false_blocks(reps=1000)
    resubmission(reps=600, k=20, n=400)
