"""Pilot sizing: how much dispute history and how many shadow weeks a pilot needs.

A thin wrapper over `scruple.stats.planning` (docs/math/03_sample_size.md). The partner
gives three things: monthly dispute volume, the reason-code mix, and the smallest
per-case improvement worth detecting. The planner returns the cases needed so that an
improvement of that size in *every* reason code is detected with the planned power,
while the chance of a false "better" in any reason code stays at most `alpha`.

Reading of the planner: `scruple.stats.planning` plans for a one-sided shift of the mean
paired difference. Here the paired difference is baseline cost minus candidate cost
(per case, USD minor units), so its "regression" is the candidate's improvement. The
maths is the same; only the sign convention changes.

What the numbers are:
- `months_of_history`: months of *resolved* disputes the backtest must replay. Cases
  still open at export have no outcome and do not count.
- `shadow_weeks_traffic`: weeks of live traffic the shadow run must see.
- `shadow_weeks_total`: that plus `outcome_lag_weeks`, the wait before those cases
  resolve. The lag is an input, not a fact we know: ask the partner for their own
  median time to final outcome.

Every input is the partner's or an ASSUMPTION; the example in docs/pilot/
SUCCESS_CRITERIA.md labels its inputs as illustrative.

Run:
    uv run python scripts/pilot_plan.py --monthly-volume 4000 \\
        --mix fraud=0.5,not_received=0.3,not_as_described=0.2 \\
        --min-improvement 5 --cost-cap 100 --sd 20
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scruple.stats.planning import StratumSpec, cases_lower_bound, plan

WEEKS_PER_MONTH = 52.0 / 12.0
MIX_TOLERANCE = 1e-6
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class PilotInputs(BaseModel):
    """What the partner tells us. Money in whole US dollars per case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly_volume: int = Field(gt=0, le=100_000_000)
    mix: dict[str, float] = Field(min_length=1, max_length=64)
    min_improvement_usd: float = Field(gt=0.0)
    cost_cap_usd: float = Field(gt=0.0)
    sd_usd: float | None = Field(default=None, gt=0.0)  # per-case sd of the difference
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    beta: float = Field(default=0.2, gt=0.0, lt=1.0)
    gamma: float = Field(default=0.05, gt=0.0, lt=1.0)
    method: Literal["bernstein", "hoeffding", "clt"] = "bernstein"
    allocation: Literal["uniform", "optimised"] = "optimised"
    shadow_fraction: float = Field(default=1.0, gt=0.0, le=1.0)
    outcome_lag_weeks: float = Field(default=0.0, ge=0.0, le=104.0)
    shadow_window_weeks: float = Field(default=3.0, gt=0.0, le=52.0)

    @field_validator("mix")
    @classmethod
    def _mix_valid(cls, v: dict[str, float]) -> dict[str, float]:
        for name, share in v.items():
            if not re.fullmatch(NAME_PATTERN, name):
                raise ValueError(f"reason code name {name!r} must match {NAME_PATTERN}")
            if not (0.0 < share <= 1.0) or math.isnan(share):
                raise ValueError(f"share for {name!r} must lie in (0, 1]")
        total = math.fsum(v.values())
        if abs(total - 1.0) > MIX_TOLERANCE:
            raise ValueError(f"reason-code shares must sum to 1 (got {total:.6f})")
        return v

    @model_validator(mode="after")
    def _within_cap(self) -> PilotInputs:
        if self.min_improvement_usd >= self.cost_cap_usd:
            raise ValueError("min_improvement_usd must be below cost_cap_usd")
        return self


class ReasonPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    share: float
    alpha: float
    cases_needed: int  # cases of this reason code
    total_cases: int  # all cases so this reason code reaches cases_needed w.p. 1 - gamma
    lower_bound_cases: int  # no valid nonparametric test needs fewer (cap tax)


class PilotPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: PilotInputs
    reasons: tuple[ReasonPlan, ...]
    binding_reason: str
    total_cases: int
    months_of_history: float
    shadow_weeks_traffic: float
    shadow_weeks_total: float
    shadow_fits_window: bool


def _round_up(x: float, places: int = 1) -> float:
    f = 10.0**places
    return math.ceil(x * f - 1e-9) / f


def pilot_plan(inp: PilotInputs) -> PilotPlan:
    """Cases, months of history and shadow weeks for one pilot."""
    names = sorted(inp.mix)  # stable order, independent of how the mix was typed
    strata = [
        StratumSpec(
            name=n,
            frequency=inp.mix[n],
            cost_cap=inp.cost_cap_usd * 100,
            min_detectable_regression=inp.min_improvement_usd * 100,
            sd=None if inp.sd_usd is None else inp.sd_usd * 100,
        )
        for n in names
    ]
    p = plan(
        strata,
        alpha=inp.alpha,
        beta=inp.beta,
        gamma=inp.gamma,
        method=inp.method,
        allocation=inp.allocation,
    )
    reasons = tuple(
        ReasonPlan(
            reason=sp.name,
            share=spec.frequency,
            alpha=sp.alpha,
            cases_needed=sp.stratum_cases,
            total_cases=sp.total_cases,
            lower_bound_cases=math.ceil(
                cases_lower_bound(spec.gap, spec.null_mean, sp.alpha, inp.beta)
            ),
        )
        for sp, spec in zip(p.strata, strata, strict=True)
    )
    weekly = inp.monthly_volume / WEEKS_PER_MONTH * inp.shadow_fraction
    traffic_weeks = _round_up(p.total_cases / weekly)
    total_weeks = _round_up(traffic_weeks + inp.outcome_lag_weeks)
    return PilotPlan(
        inputs=inp,
        reasons=reasons,
        binding_reason=p.binding_stratum,
        total_cases=p.total_cases,
        months_of_history=_round_up(p.total_cases / inp.monthly_volume),
        shadow_weeks_traffic=traffic_weeks,
        shadow_weeks_total=total_weeks,
        shadow_fits_window=total_weeks <= inp.shadow_window_weeks,
    )


def parse_mix(text: str) -> dict[str, float]:
    """`fraud=0.5,not_received=0.5` -> dict. Raises ValueError on bad input."""
    out: dict[str, float] = {}
    for part in text.split(","):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            raise ValueError(f"mix entry {part!r} must look like name=share")
        if name in out:
            raise ValueError(f"reason code {name!r} appears twice")
        try:
            share = float(value)
        except ValueError:
            raise ValueError(f"share {value!r} for {name!r} is not a number") from None
        if not math.isfinite(share):
            raise ValueError(f"share for {name!r} must be finite")
        out[name] = share
    return out


def render(pp: PilotPlan) -> str:
    i = pp.inputs
    lines = [
        "pilot plan (inputs are the partner's or illustrative; no outcome is predicted)",
        f"  monthly volume {i.monthly_volume:,}, detect ${i.min_improvement_usd:g} per case, "
        f"cap ${i.cost_cap_usd:g}, sd " + ("worst case" if i.sd_usd is None else f"${i.sd_usd:g}"),
        f"  alpha {i.alpha:g} (family-wise), power {1 - i.beta:g} per reason code, "
        f"count risk gamma {i.gamma:g}, {i.method}, {i.allocation} allocation",
        "",
        f"  {'reason':<20} {'share':>6} {'alpha_k':>8} {'cases':>7} {'total':>8} {'floor':>6}",
    ]
    for r in pp.reasons:
        lines.append(
            f"  {r.reason:<20} {r.share:>6.3f} {r.alpha:>8.2g} {r.cases_needed:>7,} "
            f"{r.total_cases:>8,} {r.lower_bound_cases:>6,}"
        )
    lines += [
        "",
        f"  total resolved cases needed: {pp.total_cases:,} (binding: {pp.binding_reason})",
        f"  months of resolved history:  {pp.months_of_history:g}",
        f"  shadow weeks of traffic:     {pp.shadow_weeks_traffic:g} "
        f"(shadowing {i.shadow_fraction:.0%} of traffic)",
        f"  shadow weeks incl. lag:      {pp.shadow_weeks_total:g} "
        f"(outcome lag {i.outcome_lag_weeks:g} weeks)",
        f"  fits a {i.shadow_window_weeks:g}-week shadow window: "
        + ("yes" if pp.shadow_fits_window else "no"),
        "",
        "  cases = cases of that reason code; total = all cases so that reason code",
        "  reaches its count with probability 1 - gamma; floor = fewest cases any valid",
        "  nonparametric test could use at that level (docs/math/03, Proposition 3.3).",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pilot_plan", description=__doc__.split("\n")[0])
    ap.add_argument("--monthly-volume", type=int, required=True, help="disputes per month")
    ap.add_argument("--mix", required=True, help="reason=share,... (shares sum to 1)")
    ap.add_argument("--min-improvement", type=float, required=True, help="USD per case")
    ap.add_argument("--cost-cap", type=float, required=True, help="max |cost difference|, USD")
    ap.add_argument("--sd", type=float, default=None, help="per-case sd of the difference, USD")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--beta", type=float, default=0.2)
    ap.add_argument("--gamma", type=float, default=0.05)
    ap.add_argument("--method", choices=["bernstein", "hoeffding", "clt"], default="bernstein")
    ap.add_argument("--allocation", choices=["uniform", "optimised"], default="optimised")
    ap.add_argument("--shadow-fraction", type=float, default=1.0)
    ap.add_argument("--outcome-lag-weeks", type=float, default=0.0)
    ap.add_argument("--shadow-window-weeks", type=float, default=3.0)
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inp = PilotInputs(
            monthly_volume=args.monthly_volume,
            mix=parse_mix(args.mix),
            min_improvement_usd=args.min_improvement,
            cost_cap_usd=args.cost_cap,
            sd_usd=args.sd,
            alpha=args.alpha,
            beta=args.beta,
            gamma=args.gamma,
            method=args.method,
            allocation=args.allocation,
            shadow_fraction=args.shadow_fraction,
            outcome_lag_weeks=args.outcome_lag_weeks,
            shadow_window_weeks=args.shadow_window_weeks,
        )
    except ValueError as e:  # pydantic.ValidationError is a ValueError
        print(f"pilot_plan: {e}", file=sys.stderr)
        return 2
    pp = pilot_plan(inp)
    print(json.dumps(pp.model_dump(mode="json"), indent=2) if args.json else render(pp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
