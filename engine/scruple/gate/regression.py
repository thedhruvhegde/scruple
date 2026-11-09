"""Golden regression suite: hand-written cases with the actions a policy owner allows.

Golden cases encode *expected behaviour given observable facts* ("a duplicate must
be contested", "late evidence must be accepted"), not the luck of one hidden
ground-truth draw. Scoring a probabilistic rule against single ground-truth
outcomes would fail every useful rule; that is what the statistical replay is for.
(Finding F-5 in docs/VERIFIER_FINDINGS.md.) The suite is append-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scruple.domain import Action, ReasonCode
from scruple.sim.models import DisputeCase, Scenario

A = Action

_TEMPLATE: dict[str, Any] = {
    "merchant_id": "m-golden",
    "mcc": "5732",
    "reason_code": ReasonCode.NOT_RECEIVED,
    "amount_minor": 12_000,
    "dispute_currency": "USD",
    "txn_currency": "USD",
    "captured_minor": 12_000,
    "refunded_minor": 0,
    "delivery_confirmed": True,
    "three_ds": False,
    "evidence_ready_day": 5,
    "response_deadline_day": 30,
    "prior_dispute_on_txn": False,
    "scenario": Scenario.NOT_RECEIVED,
    "claim_valid": False,
}


@dataclass(frozen=True)
class GoldenCase:
    case: DisputeCase
    allowed: frozenset[Action]
    why: str


def _g(name: str, allowed: set[Action], why: str, **kw: Any) -> GoldenCase:
    body = {**_TEMPLATE, "case_id": f"golden-{name}", "txn_id": f"txn-golden-{name}", **kw}
    return GoldenCase(DisputeCase(**body), frozenset(allowed), why)


ANY = {A.ACCEPT, A.CONTEST, A.ESCALATE}


def golden_cases() -> list[GoldenCase]:
    return [
        _g(
            "late-evidence",
            {A.ACCEPT, A.ESCALATE},
            "cannot win without evidence in time",
            evidence_ready_day=35,
            response_deadline_day=30,
            claim_valid=True,
            scenario=Scenario.LATE_EVIDENCE,
        ),
        _g(
            "late-evidence-short-window",
            {A.ACCEPT, A.ESCALATE},
            "deadline already passed",
            evidence_ready_day=33,
            response_deadline_day=20,
            scenario=Scenario.LATE_EVIDENCE,
        ),
        _g(
            "partial-refund",
            {A.CONTEST, A.ESCALATE},
            "accepting would refund twice",
            reason_code=ReasonCode.NOT_AS_DESCRIBED,
            refunded_minor=4_000,
            claim_valid=True,
            scenario=Scenario.PARTIAL_REFUND,
        ),
        _g(
            "duplicate-original",
            ANY,
            "first dispute: any action is defensible",
            txn_id="txn-golden-dup",
            claim_valid=True,
        ),
        _g(
            "duplicate-second",
            {A.CONTEST, A.ESCALATE},
            "never pay the same dispute twice",
            txn_id="txn-golden-dup",
            prior_dispute_on_txn=True,
            scenario=Scenario.DUPLICATE_DISPUTE,
        ),
        _g(
            "fx-mismatch",
            {A.CONTEST, A.ESCALATE},
            "converted amount exceeds capture",
            reason_code=ReasonCode.FRAUD,
            txn_currency="EUR",
            captured_minor=11_111,
            amount_minor=12_480,
            claim_valid=True,
            scenario=Scenario.CURRENCY_MISMATCH,
        ),
        _g(
            "high-value",
            {A.ESCALATE},
            "over $10,000 needs a human",
            amount_minor=2_500_000,
            captured_minor=2_500_000,
            scenario=Scenario.HIGH_VALUE,
        ),
        _g(
            "fraud-3ds",
            {A.CONTEST, A.ESCALATE},
            "3-D Secure shifts liability",
            reason_code=ReasonCode.FRAUD,
            three_ds=True,
            scenario=Scenario.FRAUD,
        ),
        _g("inr-delivered", ANY, "judgement call: statistical replay decides", claim_valid=False),
        _g(
            "small-inr-undelivered",
            ANY,
            "judgement call",
            delivery_confirmed=False,
            amount_minor=600,
            captured_minor=600,
            claim_valid=True,
        ),
    ]
