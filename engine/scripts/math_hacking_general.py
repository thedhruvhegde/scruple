"""Leaky audits and an imperfect verifier on the E2 policies (docs/math/05, 5.5-5.8).

Run: uv run python scripts/math_hacking_general.py   (seeded; about a minute)

Reads the hacked counts per batch of 200 benchmark cases that E2 recorded for every
policy its proxy climb visited (results/experiments/E2_reward_hacking.json; no salt
needed) and checks Theorems 5.5 and 5.7 on them. Synthetic episodes only: the counts
come from the scruple simulator; the audit, leak and verifier error rates are swept
(ASSUMPTION), except the false-alarm rate 0.0019, which is the honest reference
policy's measured hard-failure rate on benchmark v1 (19 of 10,000 cases).
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from scruple.stats.hacking_general import (
    LeakyAudit,
    best_response,
    deterrable,
    expected_gap_bound,
    leak_aware_outcomes,
    learning_optimiser,
    minimal_audit_rate,
    play_leaky_round,
    tail_bound,
)

ROOT = Path(__file__).resolve().parents[1]
E2 = ROOT / "results" / "experiments" / "E2_reward_hacking.json"
N = 200
GAMMAS = (0.01, 0.02, 0.05, 0.1)
PHI_HONEST = 19 / 10_000  # honest reference, INV-01 failures on benchmark v1
SEED = 20261110


def e2_batches() -> list[tuple[str, int]]:
    data = json.loads(E2.read_text())
    out: list[tuple[str, int]] = []
    for name, rows in data["bounds"]["per_policy"].items():
        seen: dict[int, int] = {}
        for i, r in enumerate(rows):
            seen.setdefault(i % 10, int(r["hacked"]))
        out.extend((name, m) for m in seen.values())
    return out


def exact_leak_aware(m: int, audit: LeakyAudit) -> list[float]:
    return leak_aware_outcomes(N, m, audit)


def bound_checks() -> None:
    batches = e2_batches()
    hacked = [m for _, m in batches if m > 0]
    print(f"## E2 batches: {len(batches)} (policy, batch) pairs of n={N}; {len(hacked)} with hacks")
    print(f"hacked counts: min {min(hacked)}, max {max(hacked)}")
    print("leak-aware policy hacks only when its signal says 'not audited'; exact probabilities")
    print(
        "rho = (1-beta) q l/(q l + 1 - q) (Theorem 5.5); naive rho = (1-beta) q ignores the leak\n"
    )
    for q in (0.05, 0.2):
        for beta in (0.0, 0.3):
            for ell in (1.0, 0.5, 0.2):
                for phi in (0.0, PHI_HONEST):
                    audit = LeakyAudit(rate=q, false_negative=beta, false_alarm=phi, leak=ell)
                    rho = audit.catch_floor
                    naive = (1 - beta) * q
                    eb = expected_gap_bound(N, 1.0, rho)
                    eb_naive = expected_gap_bound(N, 1.0, naive)
                    worst_e = 0.0
                    worst_tail_ratio = 0.0
                    tails = holds = naive_violations = 0
                    for m in hacked:
                        pr = exact_leak_aware(m, audit)
                        e_gap = math.fsum(h / N * p for h, p in enumerate(pr))
                        worst_e = max(worst_e, e_gap)
                        for g in GAMMAS:
                            tail = math.fsum(p for h, p in enumerate(pr) if h / N >= g - 1e-12)
                            b = tail_bound(g, N, 1.0, rho)
                            tails += 1
                            holds += tail <= b + 1e-12
                            if b > 0:
                                worst_tail_ratio = max(worst_tail_ratio, tail / b)
                            naive_violations += tail > tail_bound(g, N, 1.0, naive) + 1e-12
                    print(
                        f"q={q:<4} beta={beta:<3} l={ell:<3} phi={phi:<6}: rho={rho:.4f}; "
                        f"max E[Gamma 1(acc)]={worst_e:.5f} vs bound {eb:.5f} "
                        f"(ratio {worst_e / eb:.3f}; naive bound {eb_naive:.5f}"
                        f"{' VIOLATED' if worst_e > eb_naive + 1e-12 else ''}); "
                        f"tail holds {holds}/{tails} (max ratio {worst_tail_ratio:.3f}), "
                        f"naive tail violated {naive_violations}/{tails}"
                    )


def mc_agrees() -> None:
    """The simulator agrees with the exact formula (one batch, seeded)."""
    m, reps = 109, 20_000
    audit = LeakyAudit(rate=0.05, false_negative=0.3, false_alarm=PHI_HONEST, leak=0.5)
    pr = exact_leak_aware(m, audit)
    rng = random.Random(SEED)
    acc = 0
    gap = 0.0
    for _ in range(reps):
        r = play_leaky_round(N, range(m), audit, rng, leak_aware=True)
        acc += r.accepted
        gap += r.gap * r.accepted
    se = math.sqrt(math.fsum(pr) * (1 - math.fsum(pr)) / reps)
    print(
        f"\n## simulator vs exact (m={m}, q=0.05, beta=0.3, l=0.5, phi={PHI_HONEST}, reps={reps})\n"
        f"P(acc): simulated {acc / reps:.4f}, exact {math.fsum(pr):.4f} (SE {se:.4f}); "
        f"E[Gamma 1(acc)]: simulated {gap / reps:.5f}, "
        f"exact {math.fsum(h / N * p for h, p in enumerate(pr)):.5f}"
    )


def audit_rates() -> None:
    v, g, m_max = 0.449, 1 / (2 * N), 109
    print("\n## minimal audit rate on the E2 proxy (Theorem 5.7)")
    print(
        f"proxy = mean(engine win rate, self-reported win rate); a false win claim adds "
        f"g = 1/(2n) = {g}; honest value V = {v} (honest engine win rate on v1); "
        f"up to {m_max} hackable cases per batch (claim_rate = 1)"
    )
    for c in (0.0, v):
        for beta in (0.0, 0.25, 0.5):
            for phi in (0.0, PHI_HONEST, 0.05):
                qs = minimal_audit_rate(v, c, g, false_negative=beta, false_alarm=phi)
                line = f"C={c:<5} beta={beta:<4} phi={phi:<6}: q*={qs:.5f}"
                if qs <= 1.0:
                    picks = []
                    for f in (0.5, 0.99, 1.01, 2.0):
                        q = min(1.0, f * qs)
                        audit = LeakyAudit(rate=q, false_negative=beta, false_alarm=phi)
                        m_star, _ = best_response(N, [g] * m_max, audit, v, c)
                        picks.append(f"{f}q* -> m*={m_star}")
                    line += "; best response " + ", ".join(picks)
                print(line)
    print("\n## no audit rate deters (Corollary 5.8): beta g > (1 - beta - phi) K")
    for v2, c2, g2, beta, phi in (
        (1.0, 0.0, 1.0, 0.5, 0.0),
        (1.0, 0.0, 1.0, 0.6, 0.0),
        (1.0, 0.0, 0.1, 0.5, 0.5),
        (1.0, 1.0, 0.1, 0.0, 0.95),
    ):
        qs = minimal_audit_rate(v2, c2, g2, false_negative=beta, false_alarm=phi)
        ok = deterrable(v2, c2, g2, false_negative=beta, false_alarm=phi)
        print(f"V={v2} C={c2} g={g2} beta={beta} phi={phi}: q*={qs:.3f}, deterrable={ok}")


def learner() -> None:
    v, c, g, n, m_max = 10.0, 5.0, 1.0, 200, 50
    print(f"\n## learning optimiser with an imperfect verifier (V={v}, C={c}, g={g}, n={n})")
    for beta, phi in ((0.0, 0.0), (0.3, 0.0), (0.3, PHI_HONEST), (0.3, 0.01)):
        qs = minimal_audit_rate(v, c, g, false_negative=beta, false_alarm=phi)
        out = []
        for f in (0.5, 1.0, 2.0):
            q = min(1.0, f * qs)
            audit = LeakyAudit(rate=q, false_negative=beta, false_alarm=phi)
            m_star, _ = best_response(n, [g] * m_max, audit, v, c)
            pay, arm = learning_optimiser(
                n, g, m_max, audit, v, c, rounds=6000, seed=SEED, arms=(0, 1, 2, 5, 10, 20, 50)
            )
            out.append(f"{f}q*: m*={m_star}, learner arm {arm} (pays {pay:.2f})")
        print(f"beta={beta} phi={phi}: q*={qs:.4f}; " + "; ".join(out))


if __name__ == "__main__":
    bound_checks()
    mc_agrees()
    audit_rates()
    learner()
