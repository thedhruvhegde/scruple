"""Gate thresholds. Defaults are illustrative, not calibrated (docs/ASSUMPTIONS.md)."""

from __future__ import annotations

from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scruple.sim.generator import DEFAULT_PROFILE, Profile
from scruple.sim.models import (
    ARBITRATION_FEE_USD,
    ESCALATION_COST_USD,
    HIGH_VALUE_THRESHOLD_USD,
    REPRESENTMENT_LOSS_FEE_USD,
)

# ASSUMPTION: a policy change can move a case's cost by at most its amount plus
# every fee and the escalation cost (synthetic fee schedule in scruple.sim.models).
EVALUE_FEES_USD_MINOR = ARBITRATION_FEE_USD + REPRESENTMENT_LOSS_FEE_USD + ESCALATION_COST_USD


class GateConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Held-out evaluation set. The seed is operator-held: reports show only a
    # commitment (hash) so an agent cannot regenerate the held-out cases.
    eval_seed: int = 90_210
    n_cases: int = Field(default=3_000, ge=100, le=100_000)
    profile: Profile = DEFAULT_PROFILE

    # Paired comparison
    bootstrap_resamples: int = Field(default=2_000, ge=100)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)
    stats_seed: int = 7
    min_affected_cases: int = Field(default=30, ge=1)
    min_mean_improvement_usd_minor: float = 0.0  # CI lower bound must exceed this

    # Statistics engine. "bootstrap": the seeded percentile bootstrap plus the
    # zero-tolerance slice check (default). "evalue": anytime-valid betting
    # e-processes (scruple.stats) with per-case-type e-BH, sample-size planning and a
    # sequential canary. See docs/DESIGN.md "Statistics" and docs/DECISIONS.md.
    stats: Literal["bootstrap", "evalue"] = "bootstrap"
    # Declared bound on |candidate - baseline| loss per case for the e-value provider
    # when no per-case-type cap applies (USD minor). ASSUMPTION: the largest synthetic
    # amount ($50,000) plus every fee and the escalation cost. Must be set before
    # outcomes are seen; a case that breaks it fails the gate closed.
    evalue_cost_cap_usd_minor: float = Field(default=5_030_000.0, gt=0.0)
    # Case types for the stratified test are scenario x amount band. A band
    # "<$X" has the declared cap X + fees; amounts at or above the last edge use
    # `evalue_cost_cap_usd_minor`. Caps come from observable amounts only. The
    # default edge $80 is the synthetic generator's median amount (it splits
    # ordinary amounts in two), and $10,000 is the high-value threshold.
    evalue_band_edges_usd_minor: tuple[int, ...] = (8_000, HIGH_VALUE_THRESHOLD_USD)
    evalue_fees_usd_minor: int = Field(default=EVALUE_FEES_USD_MINOR, ge=0)
    # Per-case-type regression thresholds: a type is flagged only if its mean loss
    # worsens by more than its tolerance (USD minor per case). Keys are a scenario
    # ("late_evidence") or a full case type ("late_evidence/<$80"); the full type
    # wins. Replaces the zero-tolerance slice check when stats = "evalue".
    evalue_tolerance_usd_minor: float = Field(default=0.0, ge=0.0)
    evalue_type_tolerances_usd_minor: dict[str, float] = Field(default_factory=dict)
    # e-BH level for flagging regressed case types (false-block rate when no type
    # regressed), and for certifying improved case types.
    evalue_alpha: float = Field(default=0.05, gt=0.0, lt=1.0)
    evalue_alpha_certify: float = Field(default=0.05, gt=0.0, lt=1.0)
    # Sample-size planning (scruple.stats.plan), done before outcomes are seen: each
    # case type the candidate touches must hold enough held-out cases to flag a
    # regression of `min detectable regression` beyond its tolerance with power
    # 1 - beta, else the verdict is "insufficient evidence" (HOLD) with the
    # required n. MDR and the per-case sd are fractions of each type's declared
    # cap; the sd is an ASSUMPTION about the change's spread (checked after the
    # run and reported, never used to decide). Overrides: scenario or full type.
    evalue_min_detectable_regression_fraction: float = Field(default=0.025, gt=0.0, lt=1.0)
    evalue_type_min_detectable_regression_usd_minor: dict[str, float] = Field(default_factory=dict)
    evalue_planning_sd_fraction: float = Field(default=0.05, gt=0.0, le=1.0)
    evalue_beta: float = Field(default=0.2, gt=0.0, lt=1.0)

    # Regression guards (hard)
    slice_tolerance_usd_minor: float = 0.0  # any slice-mean worsening blocks
    max_escalation_rate_increase: float = 0.02

    # Canary
    canary_seed: int = 31_337
    canary_n_cases: int = Field(default=4_000, ge=100)
    canary_fraction: float = Field(default=0.10, gt=0.0, le=0.5)
    canary_profile: Profile = DEFAULT_PROFILE
    canary_min_matched: int = 20
    canary_tolerance_usd_minor: float = 500.0  # treatment mean cost may exceed control by this

    # Who may approve activation or roll back (segregation of duties enforced too).
    approvers: tuple[str, ...] = ("model-risk@demo.bank",)

    @field_validator("evalue_band_edges_usd_minor")
    @classmethod
    def _edges(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v or v[0] <= 0 or any(b <= a for a, b in pairwise(v)):
            raise ValueError("band edges must be positive and strictly increasing")
        return v

    @field_validator("evalue_type_tolerances_usd_minor")
    @classmethod
    def _tolerances(cls, v: dict[str, float]) -> dict[str, float]:
        if any(t < 0 for t in v.values()):
            raise ValueError("type tolerances must be non-negative")
        return v

    @field_validator("evalue_type_min_detectable_regression_usd_minor")
    @classmethod
    def _mdr(cls, v: dict[str, float]) -> dict[str, float]:
        if any(t <= 0 for t in v.values()):
            raise ValueError("minimum detectable regressions must be positive")
        return v
