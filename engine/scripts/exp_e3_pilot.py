"""Pilot sizing before and after variance reduction (docs/math/07, section 7.6).

The same question as scripts/pilot_plan.py (H5 in docs/pilot/SUCCESS_CRITERIA.md: an
improvement of at least the MDI in every reason code, family-wise alpha), planned
three ways:

- paired, as today: the per-case sd of the paired difference (common random numbers:
  same case, same outcome draws);
- no common random numbers: the same cases, but the counterfactual outcome drawn
  independently of the realised one, with the sd inflated by ``1 / sqrt(ratio)``
  (``ratio`` = variance with common draws over variance with independent draws; E3
  measured 0.0049 on synthetic episodes for R5, an input here, not a fact about a
  partner);
- paired plus control variates with a known covariate mean (Proposition 7.3): residual
  sd ``sd sqrt(1 - R^2)`` and the cap widened to ``cap (1 + 2 theta_max)`` for the
  larger bet range. ``R^2`` is an ASSUMPTION the partner's history would have to
  support, fixed before any shadow outcome.

Every input is the partner's or illustrative. Run:

    uv run python scripts/exp_e3_pilot.py --monthly-volume 4000 \\
        --mix fraud=0.5,not_received=0.3,not_as_described=0.2 \\
        --min-improvement 5 --cost-cap 100 --sd 20 --variance-ratio 0.05
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from scruple.stats.planning import Plan, StratumSpec, plan

WEEKS_PER_MONTH = 52.0 / 12.0


def _pilot_module() -> ModuleType:
    path = Path(__file__).resolve().parent / "pilot_plan.py"
    spec = importlib.util.spec_from_file_location("pilot_plan", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pilot_plan"] = mod
    spec.loader.exec_module(mod)
    return mod


def _plan(
    mix: dict[str, float], mdi: float, cap: float, sd: float, alpha: float, beta: float
) -> Plan:
    strata = [
        StratumSpec(
            name=k,
            frequency=mix[k],
            cost_cap=cap * 100,
            min_detectable_regression=mdi * 100,
            sd=sd * 100,
        )
        for k in sorted(mix)
    ]
    return plan(strata, alpha=alpha, beta=beta, method="bernstein", allocation="optimised")


def _row(label: str, p: Plan, monthly: float, lag: float) -> str:
    months = math.ceil(p.total_cases / monthly * 10 - 1e-9) / 10
    weeks = math.ceil(p.total_cases / (monthly / WEEKS_PER_MONTH) * 10 - 1e-9) / 10
    return (
        f"  {label:<52} {p.total_cases:>9,} {months:>8g} {weeks:>8g} {weeks + lag:>8g}"
        f"  ({p.binding_stratum})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    pp = _pilot_module()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--monthly-volume", type=int, required=True)
    ap.add_argument("--mix", required=True)
    ap.add_argument("--min-improvement", type=float, required=True)
    ap.add_argument("--cost-cap", type=float, required=True)
    ap.add_argument("--sd", type=float, required=True)
    ap.add_argument("--variance-ratio", type=float, required=True)
    ap.add_argument("--r2", type=float, nargs="*", default=[0.25, 0.5, 0.75])
    ap.add_argument("--theta-max", type=float, default=0.5)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--beta", type=float, default=0.2)
    ap.add_argument("--outcome-lag-weeks", type=float, default=0.0)
    a = ap.parse_args(argv)
    mix = pp.parse_mix(a.mix)
    base = pp.pilot_plan(
        pp.PilotInputs(
            monthly_volume=a.monthly_volume,
            mix=mix,
            min_improvement_usd=a.min_improvement,
            cost_cap_usd=a.cost_cap,
            sd_usd=a.sd,
            alpha=a.alpha,
            beta=a.beta,
            outcome_lag_weeks=a.outcome_lag_weeks,
        )
    )
    print("pilot plan before and after variance reduction (illustrative inputs; no outcome")
    print("is predicted). H5: improvement in every reason code, bernstein, optimised alpha.")
    print(
        f"  monthly volume {a.monthly_volume:,}, detect ${a.min_improvement:g}/case, "
        f"cap ${a.cost_cap:g}, paired sd ${a.sd:g}, outcome lag {a.outcome_lag_weeks:g} weeks"
    )
    print()
    print(f"  {'design':<52} {'cases':>9} {'months':>8} {'weeks':>8} {'+lag':>8}")
    print(
        f"  {'paired (scripts/pilot_plan.py, today)':<52} {base.total_cases:>9,} "
        f"{base.months_of_history:>8g} {base.shadow_weeks_traffic:>8g} "
        f"{base.shadow_weeks_total:>8g}  ({base.binding_reason})"
    )
    lag = a.outcome_lag_weeks
    sd_unpaired = a.sd / math.sqrt(a.variance_ratio)
    cap_unpaired = max(a.cost_cap, 1.0001 * (sd_unpaired**2 + a.min_improvement**2) ** 0.5)
    print(
        _row(
            f"no common random numbers (sd ${sd_unpaired:,.0f}, ratio {a.variance_ratio:g})",
            _plan(mix, a.min_improvement, cap_unpaired, sd_unpaired, a.alpha, a.beta),
            a.monthly_volume,
            lag,
        )
    )
    widen = 1.0 + 2.0 * a.theta_max
    for r2 in a.r2:
        p = _plan(
            mix,
            a.min_improvement,
            a.cost_cap * widen,
            a.sd * math.sqrt(1.0 - r2),
            a.alpha,
            a.beta,
        )
        print(
            _row(
                f"paired + control variate, R^2 {r2:g}, theta_max {a.theta_max:g}",
                p,
                a.monthly_volume,
                lag,
            )
        )
    print()
    print("  cases = resolved cases in total; months = months of resolved history at this")
    print("  volume; weeks = weeks of shadow traffic (+lag: plus the outcome lag).")
    if cap_unpaired > a.cost_cap:
        print(f"  no-CRN row: cap raised to ${cap_unpaired:,.0f} so the sd fits under it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
