"""Issuer (and network arbitrator) behaviour with seeded acceptance odds.

Decides whether the merchant prevails at each stage from the evidence alone; it never
reads ground truth. Truth reaches it only through what the merchant holds (A-19, A-20).
Every parameter is an ASSUMPTION (docs/ASSUMPTIONS.md A-21).

    p = base[stage] + weight[stage] * coverage * (1 - base[stage]) + supporting bonus

where `coverage` is the share of the reason code's required evidence kinds that were
submitted and not caught as forged. A submitted document with a detectable problem
(`document_problems`) is caught with probability `p_detect[stage]`; a caught forgery
means the merchant loses at that stage.

Determinism: the uniform draw is a function of (seed, case id, stage) only, so the
same case gets the same draw whatever evidence is submitted. Submitting more genuine,
relevant evidence can therefore never turn a win into a loss.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from scruple.sim.evidence import EvidenceDocument, EvidenceKind, document_problems
from scruple.sim.models import Stage

if TYPE_CHECKING:  # pragma: no cover
    from scruple.sim.disputes import CardDispute

DECISION_STAGES = (Stage.INQUIRY, Stage.REPRESENTMENT, Stage.PRE_ARBITRATION, Stage.ARBITRATION)


class IssuerParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = 0
    base: dict[Stage, float] = Field(
        default_factory=lambda: {
            Stage.INQUIRY: 0.30,
            Stage.REPRESENTMENT: 0.15,
            Stage.PRE_ARBITRATION: 0.10,
            Stage.ARBITRATION: 0.05,
        }
    )
    weight: dict[Stage, float] = Field(
        default_factory=lambda: {
            Stage.INQUIRY: 0.50,
            Stage.REPRESENTMENT: 0.70,
            Stage.PRE_ARBITRATION: 0.75,
            Stage.ARBITRATION: 0.90,
        }
    )
    p_detect: dict[Stage, float] = Field(
        default_factory=lambda: {
            Stage.INQUIRY: 0.30,
            Stage.REPRESENTMENT: 0.50,
            Stage.PRE_ARBITRATION: 0.70,
            Stage.ARBITRATION: 0.95,
        }
    )
    supporting_bonus: float = Field(0.03, ge=0, le=0.1)  # per supporting kind, max 2
    cap: float = Field(0.97, gt=0, le=1)


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    odds: float
    draw: float
    caught: tuple[str, ...]  # doc ids caught as problematic
    merchant_prevails: bool


def _settled_refund(doc: EvidenceDocument, amount: int) -> bool:
    return (
        doc.kind is EvidenceKind.REFUND_RECORD
        and doc.fields.get("refund_settled") == "Y"
        and int(doc.fields.get("amount_minor", "0")) >= amount
    )


def moot(case: CardDispute, evidence: tuple[EvidenceDocument, ...]) -> bool:
    """A submitted, genuine settled refund covers the disputed amount (A-23)."""
    return any(
        _settled_refund(d, case.amount_minor)
        and not document_problems(d, case.captured_minor, case.currency, case.processed_on)
        for d in evidence
    )


class SeededIssuer:
    def __init__(self, params: IssuerParams | None = None) -> None:
        self.params = params or IssuerParams()

    def _rng(self, *key: str) -> random.Random:
        return random.Random(":".join((str(self.params.seed), *key)))

    def decide(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> Decision:
        if stage not in DECISION_STAGES:
            raise ValueError(f"the issuer does not decide in {stage.value}")
        p = self.params
        caught: list[str] = []
        clean: list[EvidenceDocument] = []
        for d in evidence:
            flawed = document_problems(d, case.captured_minor, case.currency, case.processed_on)
            roll = self._rng(case.case_id, stage.value, d.doc_id).random()
            if flawed and roll < p.p_detect[stage]:
                caught.append(d.doc_id)
            else:
                clean.append(d)
        draw = self._rng(case.case_id, stage.value).random()
        if caught:
            return Decision(
                stage=stage, odds=0.0, draw=draw, caught=tuple(caught), merchant_prevails=False
            )
        if case.ce3().eligible and any(
            d.kind is EvidenceKind.PRIOR_UNDISPUTED_HISTORY for d in clean
        ):
            # CE 3.0 met and submitted: liability moves to the issuer (S2-S4, A-28)
            return Decision(stage=stage, odds=p.cap, draw=draw, caught=(), merchant_prevails=True)
        if any(_settled_refund(d, case.amount_minor) for d in clean):
            # the cardholder already has the money back: the dispute is moot (A-23)
            return Decision(stage=stage, odds=p.cap, draw=draw, caught=(), merchant_prevails=True)
        odds = self.odds(case, stage, tuple(clean))
        return Decision(stage=stage, odds=odds, draw=draw, caught=(), merchant_prevails=draw < odds)

    def odds(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> float:
        """Chance the merchant prevails given evidence that was not caught."""
        p = self.params
        # a document that itself shows a mismatch is evidence against the merchant
        kinds = {d.kind for d in evidence if d.fields.get("matches_cardholder") != "N"}
        req = case.spec.required_evidence
        coverage = sum(1 for k in req if k in kinds) / len(req)
        support = min(2, sum(1 for k in case.spec.supporting_evidence if k in kinds))
        base = p.base[stage]
        val = base + p.weight[stage] * coverage * (1 - base) + p.supporting_bonus * support
        return min(p.cap, val)

    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        return self.decide(case, stage, evidence).merchant_prevails
