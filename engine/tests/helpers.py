from __future__ import annotations

from datetime import UTC, datetime

from scruple.domain import RuleSpec
from scruple.hashing import content_hash
from scruple.registry import Provenance, RuleStatus, RuleVersion
from scruple.sim.library import BASELINE_RULES

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def rv(
    rule_id: str,
    spec: RuleSpec,
    version: int = 1,
    status: RuleStatus = RuleStatus.CANDIDATE,
    owner: str = "ops-team",
) -> RuleVersion:
    return RuleVersion(
        rule_id=rule_id,
        version=version,
        content_hash=content_hash(spec),
        spec=spec,
        owner=owner,
        provenance=Provenance(agent_id="agent-7"),
        status=status,
        created_at=T0,
    )


def baseline_versions() -> list[RuleVersion]:
    return [rv(rid, s, status=RuleStatus.ACTIVE) for rid, s in BASELINE_RULES.items()]
