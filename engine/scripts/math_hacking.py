"""Synthetic evidence for docs/math/05_hacking_gap.md.

Run: uv run python scripts/math_hacking.py   (seeded; under a minute)
"""

from __future__ import annotations

import random

from scruple.stats.hacking import (
    AuditModel,
    Incentive,
    available_inflation,
    best_response,
    expected_gap_bound,
    gap_tail_bound,
    hack_all,
    hack_largest,
    hack_random,
    learning_optimiser,
    minimal_audit_rate,
    play_round,
)

SEED = 20261109


def minimal_rates() -> None:
    print("## minimal audit rate q* = g/(d (V + C + g)) (illustrative cost models)")
    for v, c, g, d in (
        (10_000, 0, 100, 1.0),
        (10_000, 5_000, 100, 1.0),
        (1_000, 0, 100, 1.0),
        (1_000, 0, 100, 0.5),
        (0, 0, 100, 1.0),
        (0, 1_000, 100, 1.0),
    ):
        inc = Incentive(honest_value=v, rejection_cost=c, max_gain_per_case=g)
        print(f"V={v:>6} C={c:>5} g_max={g} d={d}: q*={minimal_audit_rate(inc, d):.4f}")


def bounds_vs_policies(reps: int) -> None:
    n, eps_max, q = 200, 1.0, 0.05
    audit = AuditModel(rate=q)
    rng = random.Random(SEED)
    eps = available_inflation(n, 0.5, eps_max, rng)
    print(f"\n## n={n}, eps_max={eps_max}, q={q}, half the cases hackable, reps={reps}")
    print(f"Theorem 2 bound on E[Gamma 1(acc)]: {expected_gap_bound(n, eps_max, audit):.5f}")
    policies = {
        "hack_all": hack_all(),
        "random_5": hack_random(5),
        "random_20": hack_random(20),
        "largest_10": hack_largest(10),
        "largest_20": hack_largest(20),
        "largest_60": hack_largest(60),
    }
    gammas = (0.01, 0.02, 0.05)
    print(
        "bounds P(acc and Gamma>=gamma): "
        + ", ".join(f"gamma={g}: {gap_tail_bound(g, n, eps_max, audit):.4f}" for g in gammas)
    )
    for name, policy in policies.items():
        res = [play_round(policy, eps, audit, rng) for _ in range(reps)]
        acc = sum(r.accepted for r in res) / reps
        egap = sum(r.gap for r in res if r.accepted) / reps
        tails = ", ".join(
            f"{sum(1 for r in res if r.accepted and r.gap >= g) / reps:.4f}" for g in gammas
        )
        print(f"{name:<11}: P(acc)={acc:.4f}, E[Gamma 1(acc)]={egap:.5f}, tails={tails}")


def scaling() -> None:
    print("\n## Theorem 2 bound eps_max/(n e ln(1/(1-q))) with eps_max=1")
    for n in (100, 1_000, 10_000):
        row = ", ".join(
            f"q={q}: {expected_gap_bound(n, 1.0, AuditModel(rate=q)):.5f}"
            for q in (0.01, 0.05, 0.2)
        )
        print(f"n={n:>6}: {row}")


def learners() -> None:
    rng = random.Random(SEED + 1)
    eps = available_inflation(200, 0.3, 1.0, rng)
    inc = Incentive(honest_value=10.0, rejection_cost=5.0, max_gain_per_case=1.0)
    q_star = minimal_audit_rate(inc)
    print(
        "\n## learning optimiser (epsilon-greedy over hack counts), V=10 C=5 g_max=1, "
        f"q*={q_star:.4f}"
    )
    for factor in (0.0, 0.25, 0.5, 0.9, 1.0, 1.1, 2.0):
        q = q_star * factor
        audit = AuditModel(rate=q)
        m_star, eu = best_response(eps, audit, inc)
        pay, arm = learning_optimiser(
            eps, audit, inc, rounds=3000, arms=(0, 1, 3, 10, 30, 60), seed=SEED + int(100 * factor)
        )
        print(
            f"q={q:.4f} ({factor:.2f} q*): best response m*={m_star}, EU*={eu:.3f}; "
            f"learner mean payoff={pay:.3f}, most chosen m={arm} (honest pays 10)"
        )


if __name__ == "__main__":
    minimal_rates()
    bounds_vs_policies(20_000)
    scaling()
    learners()
