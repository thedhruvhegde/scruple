"""Gate thresholds. Defaults are illustrative, not calibrated (docs/ASSUMPTIONS.md)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.generator import DEFAULT_PROFILE, Profile


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
