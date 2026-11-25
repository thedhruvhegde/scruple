"""What a `commerce` episode leaves behind for its verifiers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.guard.evidence import GuardEvidence
from scruple.guard.mandate import PaymentRequest
from scruple.sim.commerce.engine import CommerceOutcome, Refund
from scruple.sim.commerce.models import CommerceCase
from scruple.sim.ledger import JournalEntry
from scruple.verifiers.trace import EventKind, TraceEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CommerceClaims(BaseModel):
    """Structured claims about the purchase. Checked against the final ledger."""

    model_config = _FROZEN

    spent_minor: int | None = Field(
        default=None, ge=0, description="Net amount out of the wallet, cents."
    )
    units_bought: int | None = Field(
        default=None, ge=0, description="Units of the requested item bought and kept."
    )
    refunds_received_minor: int | None = Field(default=None, ge=0)
    duplicate_charges_outstanding: bool | None = Field(
        default=None, description="Whether any duplicate charge is still unrecovered."
    )
    task_completed: bool | None = Field(
        default=None, description="Whether the full quantity asked for was bought."
    )


class CommerceReport(BaseModel):
    model_config = _FROZEN

    explanation: str = Field(min_length=1, max_length=4_000)
    claims: CommerceClaims = CommerceClaims()


class CommerceRecord(BaseModel):
    model_config = _FROZEN

    episode_id: str
    seed: int
    case: CommerceCase  # includes the hidden truth and the user's key: verifiers only
    trace: tuple[TraceEvent, ...]
    trace_head: str
    events: tuple[dict[str, Any], ...]
    journal: tuple[JournalEntry, ...]
    requests: tuple[PaymentRequest, ...]
    evidence: tuple[GuardEvidence, ...]
    refunds: tuple[Refund, ...]
    outcome: CommerceOutcome
    report: CommerceReport
    allowed_tools: tuple[str, ...]
    reference_policy: bool = False
    lapsed: bool = False
    submitted_at_seq: int = 0  # trace events before this seq were visible at submission

    def journal_at_submit(self) -> tuple[JournalEntry, ...]:
        """The entries the agent could see when it reported (posted before submission)."""
        ids = {
            int(e.data["entry_id"])
            for e in self.trace
            if e.kind is EventKind.POSTING and e.seq < self.submitted_at_seq
        }
        return tuple(e for e in self.journal if e.entry_id in ids)

    def tool_calls(self, ok: bool | None = None) -> tuple[TraceEvent, ...]:
        return tuple(
            e for e in self.trace if e.kind is EventKind.TOOL_CALL and (ok is None or e.ok is ok)
        )

    def actions(self, kinds: Iterable[str]) -> tuple[TraceEvent, ...]:
        want = set(kinds)
        return tuple(e for e in self.tool_calls(ok=True) if e.tool in want)


__all__ = ["CommerceClaims", "CommerceRecord", "CommerceReport"]
