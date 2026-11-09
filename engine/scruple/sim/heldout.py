"""How big a held-out set must be to certify every case type (planning helper).

The stats work (docs/math/03_sample_size.md) showed the demo's held-out set is far too
small: rare types need thousands of cases. `heldout_size` turns a generator mix into
per-type frequencies and asks `scruple.stats.plan` how many cases of each type, and
how many held-out cases in total, a valid anytime test needs to flag a given
regression with the requested power. It only calls into `scruple.stats`.

Case type = case family by default (`scruple.sim.families.CaseFamily`). Money is in
USD minor units. The default cap, regression and spread ($100 cap, $5 regression,
$20 sd per case) are the illustrative values used in docs/math, not calibrated to
any portfolio (ASSUMPTION A-31).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from scruple.sim.disputes import FULL_MIX, CardDispute, CaseMix
from scruple.sim.families import CaseFamily
from scruple.stats import Plan, StratumSpec, plan

DEFAULT_CAP = 10_000  # $100 per case (A-31)
DEFAULT_REGRESSION = 500  # $5 per case
DEFAULT_SD = 2_000  # $20 per case


def mix_shares(mix: CaseMix = FULL_MIX) -> dict[str, float]:
    """Expected share of each case family under a mix with family weights."""
    if mix.family_weights is None:
        raise ValueError("the mix has no family weights; pass measured shares instead")
    total = sum(mix.family_weights.values())
    return {f.value: w / total for f, w in mix.family_weights.items() if w > 0}


def measured_shares(cases: Sequence[CardDispute]) -> dict[str, float]:
    """Observed share of each case family in a set of cases."""
    if not cases:
        raise ValueError("no cases")
    c = Counter(x.truth.family for x in cases)
    return {f.value: c[f] / len(cases) for f in CaseFamily if c[f]}


class TypeNeed(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    share: float
    type_cases: int  # cases of this type the test needs
    total_cases: int  # held-out cases so this type reaches type_cases w.p. >= 1 - gamma
    have: int | None = None  # cases of this type in a given held-out set
    enough: bool | None = None


class HeldoutSize(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: Plan
    needs: tuple[TypeNeed, ...]
    total_cases: int
    binding_type: str
    heldout_cases: int | None = None  # size of the set checked, if any

    @property
    def shortfall(self) -> tuple[TypeNeed, ...]:
        return tuple(t for t in self.needs if t.enough is False)

    def lines(self) -> list[str]:
        out = [
            f"held-out cases needed: {self.total_cases:,} (binding type: {self.binding_type}; "
            f"alpha {self.plan.alpha}, power {1 - self.plan.beta:.2f})"
        ]
        for t in sorted(self.needs, key=lambda x: -x.total_cases):
            have = "" if t.have is None else f"  have {t.have:,} {'ok' if t.enough else 'SHORT'}"
            out.append(
                f"  {t.case_type:<20} share {t.share:7.4f}  type cases {t.type_cases:>7,}  "
                f"total {t.total_cases:>9,}{have}"
            )
        return out


def heldout_size(
    shares: Mapping[str, float],
    *,
    cost_cap: float = DEFAULT_CAP,
    min_detectable_regression: float = DEFAULT_REGRESSION,
    sd: float | None = DEFAULT_SD,
    alpha: float = 0.05,
    beta: float = 0.2,
    gamma: float = 0.05,
    allocation: Literal["uniform", "optimised"] = "uniform",
    have: Mapping[str, int] | None = None,
) -> HeldoutSize:
    """Per-type and total held-out sizes from `scruple.stats.plan`.

    `have` (cases per type in an existing held-out set) adds an enough/short verdict."""
    if not shares:
        raise ValueError("at least one case type")
    specs = [
        StratumSpec(
            name=name,
            frequency=share,
            cost_cap=cost_cap,
            min_detectable_regression=min_detectable_regression,
            sd=sd,
        )
        for name, share in shares.items()
    ]
    p = plan(specs, alpha=alpha, beta=beta, gamma=gamma, allocation=allocation)
    needs = tuple(
        TypeNeed(
            case_type=s.name,
            share=shares[s.name],
            type_cases=s.stratum_cases,
            total_cases=s.total_cases,
            have=None if have is None else have.get(s.name, 0),
            enough=None if have is None else have.get(s.name, 0) >= s.stratum_cases,
        )
        for s in p.strata
    )
    return HeldoutSize(
        plan=p,
        needs=needs,
        total_cases=p.total_cases,
        binding_type=p.binding_stratum,
        heldout_cases=None if have is None else sum(have.values()),
    )


def check_heldout(cases: Sequence[CardDispute], **kwargs: object) -> HeldoutSize:
    """Plan from a held-out set's own family mix and say which types are short."""
    shares = measured_shares(cases)
    counts = Counter(c.truth.family.value for c in cases)
    return heldout_size(shares, have=dict(counts), **kwargs)  # type: ignore[arg-type]
