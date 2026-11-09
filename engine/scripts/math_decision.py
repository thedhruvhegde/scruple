"""Synthetic evidence for docs/math/04_asymmetric_decision.md.

Run: uv run python scripts/math_decision.py   (seeded; under a minute)
"""

from __future__ import annotations

from scruple.stats.decision import LossModel, NormalPrior, bayes_threshold, simulate_rules

SEED = 20261109
PRIOR = NormalPrior(mean=-0.01, sd=0.03)  # normalised units: most changes help a little


def main() -> None:
    print(
        "## prior N(-0.01, 0.03^2) on theta (normalised), tolerance 0, ternary data "
        "p_changed=0.3 |x|=0.8, reps=10000 per row"
    )
    header = "c_A:c_B  kind      alpha  n    p*      | Bayes risk: bayes / freq / composed / block"
    print(header + " | P(promote | theta=delta): bayes / freq / composed")
    rows = [
        ("zero_one", 100.0, 1.0, 0.10, 300),
        ("zero_one", 100.0, 1.0, 0.05, 300),
        ("zero_one", 10.0, 1.0, 0.05, 300),
        ("zero_one", 1.0, 1.0, 0.05, 300),
        ("zero_one", 1.0, 10.0, 0.05, 300),
        ("linear", 10.0, 1.0, 0.05, 300),
    ]
    for i, (kind, c_a, c_b, alpha, n) in enumerate(rows):
        loss = LossModel(kind=kind, wrong_promote=c_a, wrong_block=c_b, volume=100.0)  # type: ignore[arg-type]
        res = simulate_rules(PRIOR, 0.0, loss, alpha=alpha, n=n, reps=10_000, seed=SEED + i)
        r, b = res.bayes_risk, res.boundary_promote_rate
        print(
            f"{c_a:>5g}:{c_b:<3g} {kind:<9} {alpha:<5} {n:<4} {bayes_threshold(loss):.4f} | "
            f"{r['bayes']:.4f} / {r['frequentist']:.4f} / {r['composed']:.4f} / "
            f"{r['always_block']:.4f} | {b['bayes']:.4f} / {b['frequentist']:.4f} / "
            f"{b['composed']:.4f} | SE(freq - composed)={res.risk_diff_se['frequentist']:.4f}"
        )
        p = res.posterior_risk
        print(
            f"{'':>32}posterior-expected risk: {p['bayes']:.4f} / {p['frequentist']:.4f} / "
            f"{p['composed']:.4f} / {p['always_block']:.4f}"
        )


def rerun_first_row() -> None:
    loss = LossModel(wrong_promote=100.0, wrong_block=1.0)
    res = simulate_rules(PRIOR, 0.0, loss, alpha=0.10, n=300, reps=50_000, seed=SEED + 50)
    r, p = res.bayes_risk, res.posterior_risk
    print(
        "\n## row 1 again with reps=50000: realised "
        f"{r['bayes']:.4f} / {r['frequentist']:.4f} / {r['composed']:.4f}; posterior-expected "
        f"{p['bayes']:.4f} / {p['frequentist']:.4f} / {p['composed']:.4f}; "
        f"SE(freq - composed)={res.risk_diff_se['frequentist']:.4f}"
    )


if __name__ == "__main__":
    main()
    rerun_first_row()
