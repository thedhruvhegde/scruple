"""What a `payables` episode leaves behind for its verifiers (like `AchRecord`)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.ledger import JournalEntry
from scruple.sim.payables.engine import PayablesOutcome
from scruple.sim.payables.models import PayablesCase
from scruple.verifiers.trace import EventKind, TraceEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class PayablesClaims(BaseModel):
    """Structured claims about the pay run. Checked against the ledger."""

    model_config = _FROZEN

    paid_minor: int | None = Field(default=None, ge=0, description="Total paid, USD cents.")
    invoices_paid: int | None = Field(default=None, ge=0)
    invoices_held: int | None = Field(default=None, ge=0)
    invoices_rejected: int | None = Field(default=None, ge=0)
    no_losses: bool | None = Field(
        default=None, description="Whether nothing was paid to a duplicate or a fake vendor."
    )


class PayablesReport(BaseModel):
    model_config = _FROZEN

    explanation: str = Field(min_length=1, max_length=4_000)
    claims: PayablesClaims = PayablesClaims()


class PayablesRecord(BaseModel):
    model_config = _FROZEN

    episode_id: str
    seed: int
    case: PayablesCase  # includes the hidden truth: verifiers only
    trace: tuple[TraceEvent, ...]
    trace_head: str
    events: tuple[dict[str, Any], ...]
    journal: tuple[JournalEntry, ...]
    outcome: PayablesOutcome
    report: PayablesReport
    allowed_tools: tuple[str, ...]
    reference_policy: bool = False
    lapsed: bool = False

    def tool_calls(self, ok: bool | None = None) -> tuple[TraceEvent, ...]:
        return tuple(
            e for e in self.trace if e.kind is EventKind.TOOL_CALL and (ok is None or e.ok is ok)
        )

    def actions(self, kinds: Iterable[str]) -> tuple[TraceEvent, ...]:
        want = set(kinds)
        return tuple(e for e in self.tool_calls(ok=True) if e.tool in want)


__all__ = ["PayablesClaims", "PayablesRecord", "PayablesReport"]
