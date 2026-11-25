"""Evidence records for Guard decisions, hash-chained.

Each `GuardEvidence` covers one decision (and, for a step-up, the user's answer): the
mandate and request digests, the result of every rule, the verdict, the state digest
the decision was taken on, and the previous record's hash. Editing, dropping or
reordering a record breaks every hash after it (`verify_chain`).

`record_to_audit` appends the same body to scruple's tenant audit trail
(`scruple.audit.log.AuditLog`, event type `guard.decision`), so Guard decisions sit in
the same tamper-evident log as rule approvals and can be anchored with
`scruple audit anchor`. In a commerce episode the record also goes into the episode's
own hash-chained trace (`scruple.verifiers.trace`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from scruple.guard.rules import Approval, Decision, Verdict
from scruple.hashing import canonical_json, sha256_hex

if TYPE_CHECKING:  # pragma: no cover
    from scruple.audit.log import AuditLog
    from scruple.audit.models import AuditEntry

GENESIS = sha256_hex("scruple-guard/genesis/v0")
EVENT_TYPE = "guard.decision"


class GuardEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    decision: Decision
    approval: Approval | None = None
    final: Verdict
    prev_hash: str
    hash: str = ""

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"hash"})

    def computed_hash(self) -> str:
        return sha256_hex(canonical_json(self.body()))


class EvidenceChain:
    """Append-only chain of Guard evidence records (in memory)."""

    def __init__(self, records: Sequence[GuardEvidence] = ()) -> None:
        self._records: list[GuardEvidence] = list(records)

    @property
    def head(self) -> str:
        return self._records[-1].hash if self._records else GENESIS

    @property
    def records(self) -> tuple[GuardEvidence, ...]:
        return tuple(self._records)

    def append(
        self, decision: Decision, final: Verdict, approval: Approval | None = None
    ) -> GuardEvidence:
        draft = GuardEvidence(
            seq=len(self._records) + 1,
            decision=decision,
            approval=approval,
            final=final,
            prev_hash=self.head,
        )
        rec = draft.model_copy(update={"hash": draft.computed_hash()})
        self._records.append(rec)
        return rec


def verify_chain(records: Sequence[GuardEvidence], expected_head: str | None = None) -> int | None:
    """None if the chain verifies (and ends at `expected_head` if given), else the seq
    of the first bad record (0 for a head mismatch on an otherwise valid chain)."""
    prev = GENESIS
    for i, r in enumerate(records, start=1):
        if r.seq != i or r.prev_hash != prev or r.hash != r.computed_hash():
            return i
        prev = r.hash
    if expected_head is not None and prev != expected_head:
        return 0
    return None


def record_to_audit(audit: AuditLog, record: GuardEvidence, actor: str) -> AuditEntry:
    """Append a Guard evidence record to the tenant audit trail."""
    return audit.append(EVENT_TYPE, actor, record.model_dump(mode="json"))


__all__ = [
    "EVENT_TYPE",
    "GENESIS",
    "EvidenceChain",
    "GuardEvidence",
    "record_to_audit",
    "verify_chain",
]
