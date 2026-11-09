"""Run the stats adapter on the synthetic demo's held-out shadow replay (read-only).

Run: uv run python scripts/math_gate_demo.py   (seeded; about 10 seconds)

Uses the same held-out generation as gate/engine.py's evaluate_shadow, without
changing the engine. Everything is synthetic.
"""

from __future__ import annotations

import math
from collections import defaultdict

from scruple.gate.config import GateConfig
from scruple.sim.engine import Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.library import BAD_RULE, BAD_RULE_ID, GOOD_RULE, GOOD_RULE_ID, baseline_policy
from scruple.sim.models import (
    ARBITRATION_FEE_USD,
    ESCALATION_COST_USD,
    HIGH_VALUE_THRESHOLD_USD,
    REPRESENTMENT_LOSS_FEE_USD,
    to_usd,
)
from scruple.sim.policy import PolicyRule
from scruple.stats import LossPair, StatsGateConfig, StratumSpec, evaluate_pairs, plan
from scruple.stats.decision import NormalPrior
from scruple.stats.planning import cases_lower_bound
from scruple.verifiers.outcome import score_outcomes

# ASSUMPTION (declared from the generator's bounds, before looking at outcomes): the
# change in regret on a case is at most its amount plus all fees and escalation cost.
# Ordinary amounts are below $10,000; high-value ones at most 5x that.
FEES = ARBITRATION_FEE_USD + REPRESENTMENT_LOSS_FEE_USD + ESCALATION_COST_USD
BANDS = (10_000, 30_000, 100_000, HIGH_VALUE_THRESHOLD_USD)  # amount upper bounds, minor


def band_cap(amount: int, scenario: str) -> tuple[str, float]:
    if scenario == "high_value":
        return "hv", float(5 * HIGH_VALUE_THRESHOLD_USD + FEES)
    for b in BANDS:
        if amount < b:
            return f"<{b // 100}", float(b + FEES)
    return "hv", float(5 * HIGH_VALUE_THRESHOLD_USD + FEES)


def run(rule_id: str) -> tuple[list[LossPair], list[LossPair], dict[str, float]]:
    cfg = GateConfig()
    cases = generate_cases(cfg.eval_seed, cfg.n_cases, cfg.profile, prefix="h")
    base = baseline_policy()
    rule = GOOD_RULE if rule_id == GOOD_RULE_ID else BAD_RULE
    cand = base.with_rule(PolicyRule(rule_id=rule_id, ref=f"{rule_id}@v1", spec=rule))
    sim = Simulator(cases)
    b, c = score_outcomes(cases, sim.run(base)), score_outcomes(cases, sim.run(cand))
    by_scenario, by_band = [], []
    caps: dict[str, float] = {}
    for k in cases:
        s = k.scenario.value
        band, cap = band_cap(to_usd(k.amount_minor, k.dispute_currency), s)
        caps[f"{s}/{band}"] = cap
        base_loss, cand_loss = b.regret[k.case_id], c.regret[k.case_id]
        by_scenario.append(
            LossPair(
                case_id=k.case_id, case_type=s, baseline_loss=base_loss, candidate_loss=cand_loss
            )
        )
        by_band.append(
            LossPair(
                case_id=k.case_id,
                case_type=f"{s}/{band}",
                baseline_loss=base_loss,
                candidate_loss=cand_loss,
            )
        )
    return by_scenario, by_band, caps


def show(label: str, pairs: list[LossPair], cfg: StatsGateConfig) -> None:
    v = evaluate_pairs(pairs, cfg)
    print(f"## {label}: outcome={v.outcome}")
    print(
        f"n={v.n_cases}, mean diff {v.mean_diff:+.1f} minor/case, pooled CS "
        f"[{v.ci_low:+.0f}, {v.ci_high:+.0f}], certificate e={v.certify_e_value:.3g}, "
        f"pooled regression e={v.pooled_regression_e_value:.3g}"
    )
    moved = [t for t in v.types if t.mean_diff != 0]
    for t in moved:
        print(
            f"  {t.case_type:<24} n={t.n:>4} cap={t.cap / 100:>8.0f}$ mean {t.mean_diff:+8.1f} "
            f"max e={t.max_e_value:8.3g} regressed={t.regressed}"
        )
    print(f"  ({len(v.types) - len(moved)} other types unchanged)")
    print("  reasons: " + "; ".join(v.reasons))


def main() -> None:
    hv_cap = float(5 * HIGH_VALUE_THRESHOLD_USD + FEES)
    ordinary = float(HIGH_VALUE_THRESHOLD_USD + FEES)
    prior = NormalPrior(mean=0.0, sd=2_000.0)
    for label, rule_id in (("good rule", GOOD_RULE_ID), ("bad rule A-1", BAD_RULE_ID)):
        by_scenario, by_band, caps = run(rule_id)
        type_caps = {
            p.case_type: (hv_cap if p.case_type == "high_value" else ordinary) for p in by_scenario
        }
        show(
            f"{label}, strata = scenario",
            by_scenario,
            StatsGateConfig(cost_cap=hv_cap, type_caps=type_caps, prior=prior),
        )
        show(
            f"{label}, strata = scenario x amount band",
            by_band,
            StatsGateConfig(cost_cap=hv_cap, type_caps=caps, prior=prior),
        )

    # How many held-out cases would the scenario-level test need for the A-1 slice?
    _, by_band, _ = run(BAD_RULE_ID)
    diffs: dict[str, list[float]] = defaultdict(list)
    for p in by_band:
        diffs[p.case_type.split("/")[0]].append(p.diff)
    late = diffs["late_evidence"]
    mean = sum(late) / len(late)
    sd = math.sqrt(sum((d - mean) ** 2 for d in late) / (len(late) - 1))
    freq = len(late) / len(by_band)
    spec = StratumSpec(
        name="late_evidence",
        frequency=freq,
        cost_cap=ordinary,
        min_detectable_regression=mean,
        sd=sd,
    )
    for method in ("bernstein", "clt"):
        p = plan([spec], alpha=0.05 / 9, method=method)  # type: ignore[arg-type]
        print(
            f"\nplan ({method}): detect late_evidence regression of {mean / 100:.2f}$/case "
            f"(sd {sd / 100:.2f}$, freq {freq:.3f}, cap {ordinary / 100:.0f}$) at alpha 0.05/9: "
            f"{p.strata[0].stratum_cases} late-evidence cases, {p.total_cases} held-out cases"
        )
    lb = cases_lower_bound(spec.gap, 0.0, 0.05 / 9, 0.2)
    print(
        f"lower bound for any test valid over all laws on the cap: {math.ceil(lb)} late-evidence "
        f"cases (the demo has {len(late)})"
    )


if __name__ == "__main__":
    main()
