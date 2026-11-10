"""What a `recon` episode leaves behind for its verifiers (see `ach_record` for the idea)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.ledger import JournalEntry
from scruple.sim.recon.engine import ReconOutcome
from scruple.sim.recon.models import ReconCase
from scruple.sim.recon.statement import ParsedStatement
from scruple.verifiers.trace import EventKind, TraceEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ReconClaims(BaseModel):
    """Structured claims about the reconciliation. Checked against the engine."""

    model_config = _FROZEN

    balanced: bool | None = Field(
        default=None, description="Whether the statement ties out to the adjusted book."
    )
    unreconciled_minor: int | None = Field(
        default=None, description="Bank closing minus the adjusted book balance, minor units."
    )
    adjustments: int | None = Field(default=None, ge=0, description="Adjusting entries posted.")
    escalations: int | None = Field(default=None, ge=0, description="Escalations raised.")


class ReconReport(BaseModel):
    model_config = _FROZEN

    explanation: str = Field(min_length=1, max_length=4_000)
    claims: ReconClaims = ReconClaims()


class ReconRecord(BaseModel):
    model_config = _FROZEN

    episode_id: str
    seed: int
    case: ReconCase  # includes the hidden truth and the statement file
    statement: ParsedStatement  # what the agent was shown
    trace: tuple[TraceEvent, ...]
    trace_head: str
    events: tuple[dict[str, Any], ...]
    journal: tuple[JournalEntry, ...]
    outcome: ReconOutcome
    report: ReconReport
    allowed_tools: tuple[str, ...]
    reference_policy: bool = False
    lapsed: bool = False

    def tool_calls(self, ok: bool | None = None) -> tuple[TraceEvent, ...]:
        return tuple(
            e for e in self.trace if e.kind is EventKind.TOOL_CALL and (ok is None or e.ok is ok)
        )


__all__ = ["ReconClaims", "ReconRecord", "ReconReport"]
