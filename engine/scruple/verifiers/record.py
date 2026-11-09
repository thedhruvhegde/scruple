"""What a dispute episode leaves behind for the verifiers.

An `EpisodeRecord` is assembled by the environment when an episode closes. It holds
the append-only trace, the final journal for the transaction, the evidence as the
generator made it and as it ended up, the case including its hidden ground truth,
and the agent's self-report. Verifiers read only this record. The agent never sees
it and cannot write to it: its only influence is through the tool calls the trace
recorded and the self-report, which no check trusts as fact.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.disputes import CardDispute, CaseOutcome, Incident
from scruple.sim.evidence import EvidenceDocument
from scruple.sim.ledger import JournalEntry
from scruple.verifiers.trace import EventKind, TraceEvent

_FROZEN = ConfigDict(frozen=True, extra="forbid")

ClaimedOutcome = Literal["won", "partial", "lost", "accepted", "refunded", "escalated", "closed"]


class Claims(BaseModel):
    """Structured claims an agent makes about what it did. Never trusted as fact."""

    model_config = _FROZEN

    outcome: ClaimedOutcome | None = None
    refunded_minor: int | None = Field(default=None, ge=0)
    cost_usd_minor: int | None = Field(default=None, ge=0)
    actions: tuple[str, ...] = Field(default=(), max_length=20)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    win_rate: float | None = Field(default=None, ge=0, le=1)  # "I win 97% of disputes"
    deadlines_met: bool | None = None


class SelfReport(BaseModel):
    model_config = _FROZEN

    explanation: str = Field(min_length=1, max_length=4_000)
    # stated probability that the merchant ends up winning (won or partial)
    confidence: float | None = Field(default=None, ge=0, le=1)
    claims: Claims = Claims()


class BaselineResult(BaseModel):
    """The same case (same seed) replayed under a fixed reference policy."""

    model_config = _FROZEN

    policy: str
    cost_usd_minor: int
    resolution: str | None
    correct: bool


class EpisodeRecord(BaseModel):
    model_config = _FROZEN

    episode_id: str
    seed: int
    case: CardDispute  # includes hidden truth: verifiers only
    merchant_tier: str | None = None
    original_documents: tuple[EvidenceDocument, ...]  # as the generator made them
    documents: tuple[EvidenceDocument, ...]  # the case's documents at close
    trace: tuple[TraceEvent, ...]
    trace_head: str  # the environment's chain head at close
    journal: tuple[JournalEntry, ...]  # final journal entries for this transaction
    outcome: CaseOutcome | None
    incidents: tuple[Incident, ...] = ()
    report: SelfReport
    cost_usd_minor: int
    allowed_tools: tuple[str, ...]
    reference_policy: bool = False  # True only for oracle baselines (ground truth allowed)
    lapsed: bool = False
    baseline: BaselineResult | None = None

    # --- views over the trace ------------------------------------------------
    def events(self, kind: EventKind) -> tuple[TraceEvent, ...]:
        return tuple(e for e in self.trace if e.kind is kind)

    def tool_calls(self, ok: bool | None = None) -> tuple[TraceEvent, ...]:
        return tuple(e for e in self.events(EventKind.TOOL_CALL) if ok is None or e.ok is ok)

    def actions(self, kinds: Iterable[str] | None = None) -> tuple[TraceEvent, ...]:
        """Successful action tool calls (in order)."""
        want = set(ACTION_TOOLS if kinds is None else kinds)
        return tuple(e for e in self.tool_calls(ok=True) if e.tool in want)

    @property
    def resolution(self) -> str | None:
        return None if self.outcome is None else self.outcome.resolution.value

    @property
    def merchant_won(self) -> bool:
        return self.resolution in ("won", "partial")


ACTION_TOOLS = ("accept", "represent", "escalate", "refund", "request_info", "upload_evidence")


def merchant_cost(journal: Iterable[JournalEntry]) -> int:
    """What the dispute cost the merchant (settlement currency plus USD fees, minor
    units): every merchant posting after the capture, negated."""
    return -sum(
        p.amount_minor
        for e in journal
        if e.kind != "capture"
        for p in e.postings
        if p.account.startswith("merchant:")
    )


__all__ = [
    "ACTION_TOOLS",
    "BaselineResult",
    "ClaimedOutcome",
    "Claims",
    "EpisodeRecord",
    "SelfReport",
    "merchant_cost",
]
