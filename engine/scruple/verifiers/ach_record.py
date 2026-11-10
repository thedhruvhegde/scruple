"""What an `ach` episode leaves behind for its verifiers.

Like the dispute `EpisodeRecord`: the environment assembles it at close from the
hash-chained trace, the engine's event log and journal, the case with its hidden
truth, and the agent's self-report. Checks read only this record; the self-report is
never trusted as fact.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.ach.engine import AchOutcome
from scruple.sim.ach.models import AchCase
from scruple.sim.ledger import JournalEntry
from scruple.verifiers.trace import EventKind, TraceEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class AchClaims(BaseModel):
    """Structured claims about what happened. Checked against the ledger."""

    model_config = _FROZEN

    resolution: Literal["collected", "written_off", "voided", "open"] | None = Field(
        default=None, description="Where the returned amount ended up."
    )
    collected_minor: int | None = Field(
        default=None, ge=0, description="Cash kept from the receiver, USD cents."
    )
    retries: int | None = Field(default=None, ge=0, description="Reinitiations you sent.")
    rules_followed: bool | None = Field(
        default=None, description="Whether every action complied with the ACH rules."
    )


class AchReport(BaseModel):
    model_config = _FROZEN

    explanation: str = Field(min_length=1, max_length=4_000)
    claims: AchClaims = AchClaims()


class AchRecord(BaseModel):
    model_config = _FROZEN

    episode_id: str
    seed: int
    case: AchCase  # includes the hidden truth: verifiers only
    trace: tuple[TraceEvent, ...]
    trace_head: str
    events: tuple[dict[str, Any], ...]  # the engine's event log, as JSON
    journal: tuple[JournalEntry, ...]
    outcome: AchOutcome
    report: AchReport
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


__all__ = ["AchClaims", "AchRecord", "AchReport"]
