"""A versioned gate policy: one file a tenant keeps under change control.

    policy_version: 1
    name: acme-disputes
    stats:
      provider: evalue          # bootstrap | evalue | evalue_vr
      alpha: 0.05
    case_types:                 # per case-type thresholds (evalue providers only)
      late_evidence: {tolerance_usd_minor: 200}
      "fraud/<$80": {tolerance_usd_minor: 0, min_detectable_regression_usd_minor: 400}
    fairness: {profile: balanced}
    approval:                   # scruple.gate.approval.ApprovalPolicy
      approvers: [{name: alice@bank.example, roles: [model-risk]}, {name: bob@bank.example}]
      required: 2
      required_role: model-risk
    canary:                     # scruple.gate.canary.RampSchedule
      stages: [5, 10, 25, 50]

Everything is validated by pydantic (`extra="forbid"` all the way down), so a typo
is an error, not a silently ignored key. `policy_sha256` is the SHA-256 of the
canonical JSON of the validated policy: the same policy written as YAML, TOML or
JSON, in any key order, has the same hash. `gate_config` compiles a policy onto a
`GateConfig` and stamps the hash on it, so every decision, scorecard and evidence
pack made under the policy names it.

Thresholds and profiles are the tenant's configuration, not a statement of what any
institution or network requires (ASSUMPTION, as for `GateConfig`).
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scruple.gate.approval import ApprovalPolicy
from scruple.gate.canary import RampSchedule
from scruple.gate.config import GateConfig
from scruple.hashing import content_hash
from scruple.sim.models import Scenario
from scruple.verifiers.profiles import DEFAULT_PROFILE, VerifierProfile

POLICY_VERSION = 1
NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BAND = re.compile(r"^(<|>=)\$[0-9][0-9,]*(\.[0-9]{2})?$")
SCENARIOS = frozenset(s.value for s in Scenario)


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseTypeThreshold(_In):
    """One case type: a scenario (`late_evidence`) or a full type (`late_evidence/<$80`)."""

    tolerance_usd_minor: float = Field(default=0.0, ge=0.0, le=1e9)
    min_detectable_regression_usd_minor: float | None = Field(default=None, gt=0.0, le=1e9)


class StatsSection(_In):
    """The statistics provider and its levels. Unset fields keep the base config's."""

    provider: Literal["bootstrap", "evalue", "evalue_vr"] = "bootstrap"
    confidence: float | None = Field(default=None, gt=0.5, lt=1.0)
    bootstrap_resamples: int | None = Field(default=None, ge=100, le=1_000_000)
    min_affected_cases: int | None = Field(default=None, ge=1)
    alpha: float | None = Field(default=None, gt=0.0, lt=1.0)
    alpha_certify: float | None = Field(default=None, gt=0.0, lt=1.0)
    beta: float | None = Field(default=None, gt=0.0, lt=1.0)
    tolerance_usd_minor: float | None = Field(default=None, ge=0.0)
    band_edges_usd_minor: tuple[int, ...] | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("band_edges_usd_minor")
    @classmethod
    def _edges(cls, v: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if v is not None and (v[0] <= 0 or any(b <= a for a, b in pairwise(v))):
            raise ValueError("band edges must be positive and strictly increasing")
        return v


class FairnessSection(_In):
    """The verifier profile (`scruple.verifiers.profiles`, docs/DECISIONS.md D-016)."""

    profile: VerifierProfile = DEFAULT_PROFILE


def dollars(minor: int) -> str:
    """The band label format of `scruple.gate.evalue.case_type`."""
    return f"${minor // 100:,}" if minor % 100 == 0 else f"${minor / 100:,.2f}"


def band_labels(edges: tuple[int, ...]) -> tuple[str, ...]:
    return (*(f"<{dollars(e)}" for e in edges), f">={dollars(edges[-1])}")


class Policy(_In):
    policy_version: Literal[1] = 1
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default="", max_length=500)
    stats: StatsSection = StatsSection()
    case_types: dict[str, CaseTypeThreshold] = Field(default_factory=dict, max_length=200)
    fairness: FairnessSection = FairnessSection()
    approval: ApprovalPolicy | None = None
    canary: RampSchedule | None = None

    @model_validator(mode="after")
    def _case_types(self) -> Policy:
        if self.case_types and self.stats.provider == "bootstrap":
            raise ValueError(
                "case_types thresholds apply to the evalue providers only; "
                "set stats.provider to evalue or evalue_vr"
            )
        for key in self.case_types:
            scenario, _, band = key.partition("/")
            if scenario not in SCENARIOS:
                raise ValueError(
                    f"case type {key!r}: unknown scenario {scenario!r} "
                    f"(one of {', '.join(sorted(SCENARIOS))})"
                )
            if band and not _BAND.match(band):
                raise ValueError(f"case type {key!r}: band must look like <$80 or >=$10,000")
        return self

    @property
    def policy_sha256(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def gate_config(self, base: GateConfig | None = None) -> GateConfig:
        """`base` with this policy applied and its hash stamped on. Raises ValueError
        when a case-type band does not exist under the resulting band edges."""
        base = base or GateConfig()
        s = self.stats
        update: dict[str, Any] = {
            "stats": s.provider,
            "policy_sha256": self.policy_sha256,
            "verifier_profile": self.fairness.profile.value,
        }
        for field, target in (
            ("confidence", "confidence"),
            ("bootstrap_resamples", "bootstrap_resamples"),
            ("min_affected_cases", "min_affected_cases"),
            ("alpha", "evalue_alpha"),
            ("alpha_certify", "evalue_alpha_certify"),
            ("beta", "evalue_beta"),
            ("tolerance_usd_minor", "evalue_tolerance_usd_minor"),
            ("band_edges_usd_minor", "evalue_band_edges_usd_minor"),
        ):
            value = getattr(s, field)
            if value is not None:
                update[target] = value
        edges = update.get("evalue_band_edges_usd_minor", base.evalue_band_edges_usd_minor)
        labels = set(band_labels(edges))
        for key in self.case_types:
            band = key.partition("/")[2]
            if band and band not in labels:
                raise ValueError(
                    f"case type {key!r}: no band {band!r} under edges {list(edges)} "
                    f"(bands: {', '.join(band_labels(edges))})"
                )
        update["evalue_type_tolerances_usd_minor"] = {
            **base.evalue_type_tolerances_usd_minor,
            **{k: v.tolerance_usd_minor for k, v in self.case_types.items()},
        }
        update["evalue_type_min_detectable_regression_usd_minor"] = {
            **base.evalue_type_min_detectable_regression_usd_minor,
            **{
                k: v.min_detectable_regression_usd_minor
                for k, v in self.case_types.items()
                if v.min_detectable_regression_usd_minor is not None
            },
        }
        return GateConfig.model_validate({**base.model_dump(), **update})

    def ramp(self) -> RampSchedule:
        """The canary ramp this policy asks for (the default schedule if unset)."""
        return self.canary or RampSchedule()


class PolicyChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    kind: Literal["added", "removed", "changed"]
    old: Any = None
    new: Any = None

    def line(self) -> str:
        if self.kind == "added":
            return f"+ {self.path}: {self.new!r}"
        if self.kind == "removed":
            return f"- {self.path}: {self.old!r}"
        return f"~ {self.path}: {self.old!r} -> {self.new!r}"


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, dict):
        if not obj:
            return {prefix: {}} if prefix else {}
        out: dict[str, Any] = {}
        for k in sorted(obj):
            out.update(_flatten(obj[k], f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: obj}


def diff(old: Policy, new: Policy) -> tuple[PolicyChange, ...]:
    """Every leaf that differs, by dotted path (lists compare whole), sorted by path."""
    a, b = _flatten(old.model_dump(mode="json")), _flatten(new.model_dump(mode="json"))
    out: list[PolicyChange] = []
    for path in sorted(set(a) | set(b)):
        if path not in a:
            out.append(PolicyChange(path=path, kind="added", new=b[path]))
        elif path not in b:
            out.append(PolicyChange(path=path, kind="removed", old=a[path]))
        elif a[path] != b[path]:
            out.append(PolicyChange(path=path, kind="changed", old=a[path], new=b[path]))
    return tuple(out)


__all__ = [
    "POLICY_VERSION",
    "SHA256_PATTERN",
    "CaseTypeThreshold",
    "FairnessSection",
    "Policy",
    "PolicyChange",
    "StatsSection",
    "band_labels",
    "diff",
]
