"""Registry data model: versioned, content-addressed learned rules."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from scruple.domain import RuleSpec

RULE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,63}$"
ACTOR_PATTERN = r"^[A-Za-z0-9._@:-]{1,128}$"


class RuleStatus(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class Provenance(BaseModel):
    """Where a learned rule came from. Recorded, never trusted for scoring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_id: str = Field(pattern=ACTOR_PATTERN)
    episode_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    notes: str = Field(default="", max_length=2_000)
    # Agents may self-report metrics. The gate records them and ignores them
    # (see tests/test_adversarial.py::test_self_reported_metrics_are_ignored).
    claimed_metrics: dict[str, float] = Field(default_factory=dict, max_length=50)


class RuleVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(pattern=RULE_ID_PATTERN)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: RuleSpec
    owner: str = Field(pattern=ACTOR_PATTERN)
    provenance: Provenance
    status: RuleStatus
    created_at: datetime
    # Version to restore if this version is rolled back (the version it replaced).
    rollback_to: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.rule_id}@v{self.version}"
