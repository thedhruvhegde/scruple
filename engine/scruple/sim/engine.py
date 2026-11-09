"""Deterministic chargeback lifecycle simulator with snapshot/restore.

Lifecycle per case: inquiry -> chargeback -> (accept | representment [-> arbitration])
-> resolved. Outcomes are a pure function of the case's ground truth and the action,
so replaying the same cases under two policies gives a true paired comparison.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from scruple.domain import Action
from scruple.hashing import content_hash
from scruple.sim.ledger import JournalEntry, Ledger, LedgerSnapshot
from scruple.sim.models import (
    ARBITRATION_FEE_USD,
    ESCALATION_COST_USD,
    REPRESENTMENT_LOSS_FEE_USD,
    CaseResult,
    DisputeCase,
    Resolution,
    Stage,
    to_usd,
)
from scruple.sim.policy import Policy

S = Stage


class Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    stages: tuple[Stage, ...]
    resolution: Resolution
    debit_txn_minor: int
    fee_usd_minor: int


def outcome_for(case: DisputeCase, action: Action) -> Outcome:
    """Resolve a case under ACCEPT or CONTEST. ESCALATE is handled by the caller."""
    cb = case.chargeback_txn_minor
    if action is Action.ACCEPT:
        return Outcome(
            stages=(S.INQUIRY, S.CHARGEBACK, S.RESOLVED),
            resolution=Resolution.ACCEPTED,
            debit_txn_minor=cb,
            fee_usd_minor=0,
        )
    if action is not Action.CONTEST:
        raise ValueError(f"outcome_for does not handle {action}")
    if case.evidence_ready_day > case.response_deadline_day:
        # Representment rejected for missing evidence: merchant loses and pays a fee.
        return Outcome(
            stages=(S.INQUIRY, S.CHARGEBACK, S.REPRESENTMENT, S.RESOLVED),
            resolution=Resolution.LOST,
            debit_txn_minor=cb,
            fee_usd_minor=REPRESENTMENT_LOSS_FEE_USD,
        )
    liability = case.liability_txn_minor
    if liability < cb:
        res = Resolution.WON if liability == 0 else Resolution.PARTIAL
        return Outcome(
            stages=(S.INQUIRY, S.CHARGEBACK, S.REPRESENTMENT, S.RESOLVED),
            resolution=res,
            debit_txn_minor=liability,
            fee_usd_minor=0,
        )
    # Contested a fully valid claim: escalates to arbitration and loses.
    return Outcome(
        stages=(S.INQUIRY, S.CHARGEBACK, S.REPRESENTMENT, S.ARBITRATION, S.RESOLVED),
        resolution=Resolution.LOST,
        debit_txn_minor=cb,
        fee_usd_minor=ARBITRATION_FEE_USD,
    )


def cost_usd(case: DisputeCase, o: Outcome) -> int:
    return to_usd(o.debit_txn_minor, case.txn_currency) + o.fee_usd_minor


def oracle(case: DisputeCase) -> tuple[Action, Outcome, int]:
    """The best automated action with hindsight (ties prefer ACCEPT)."""
    best: tuple[Action, Outcome, int] | None = None
    for a in (Action.ACCEPT, Action.CONTEST):
        o = outcome_for(case, a)
        c = cost_usd(case, o)
        if best is None or c < best[2]:
            best = (a, o, c)
    assert best is not None
    return best


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[CaseResult, ...]
    journal: tuple[JournalEntry, ...]
    opening_entries: int

    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class SimState(BaseModel):
    model_config = ConfigDict(frozen=True)

    cursor: int
    ledger: LedgerSnapshot
    results: tuple[CaseResult, ...]


class Simulator:
    def __init__(self, cases: Sequence[DisputeCase]) -> None:
        self.cases = tuple(cases)
        self._opening = _opening_ledger(self.cases).snapshot()
        self.reset()

    def reset(self) -> None:
        self._ledger = Ledger.restore(self._opening)
        self._cursor = 0
        self._results: list[CaseResult] = []

    def snapshot(self) -> SimState:
        return SimState(
            cursor=self._cursor, ledger=self._ledger.snapshot(), results=tuple(self._results)
        )

    def restore(self, state: SimState) -> None:
        self._ledger = Ledger.restore(state.ledger)
        self._cursor = state.cursor
        self._results = list(state.results)

    @property
    def done(self) -> bool:
        return self._cursor >= len(self.cases)

    def step(self, policy: Policy) -> CaseResult:
        case = self.cases[self._cursor]
        action, decided_by = policy.decide(case.view())
        result = self._process(case, action, decided_by)
        self._results.append(result)
        self._cursor += 1
        return result

    def run(self, policy: Policy) -> RunResult:
        return self.run_routed(lambda _case: policy)

    def run_routed(self, router: Callable[[DisputeCase], Policy]) -> RunResult:
        """Run every case, choosing the policy per case (used for canary splits)."""
        self.reset()
        while not self.done:
            self.step(router(self.cases[self._cursor]))
        return self.result()

    def result(self) -> RunResult:
        return RunResult(
            results=tuple(self._results),
            journal=self._ledger.entries,
            opening_entries=len(self._opening.entries),
        )

    def _process(self, case: DisputeCase, action: Action, decided_by: str) -> CaseResult:
        _, best_outcome, best_cost = oracle(case)
        ops = 0
        if action is Action.ESCALATE:
            # Assumption: a human analyst resolves escalations optimally, at a cost.
            outcome, ops = best_outcome, ESCALATION_COST_USD
        else:
            outcome = outcome_for(case, action)
        ccy = case.txn_currency
        if outcome.debit_txn_minor > 0:
            self._ledger.post(
                "chargeback_debit",
                case.txn_id,
                case.case_id,
                ccy,
                [
                    (f"merchant:{case.merchant_id}", -outcome.debit_txn_minor),
                    (f"cardholder:{case.txn_id}", outcome.debit_txn_minor),
                ],
            )
        if outcome.fee_usd_minor > 0:
            self._ledger.post(
                "fee",
                case.txn_id,
                case.case_id,
                "USD",
                [
                    (f"merchant:{case.merchant_id}", -outcome.fee_usd_minor),
                    ("network:fees", outcome.fee_usd_minor),
                ],
            )
        cost = cost_usd(case, outcome) + ops
        return CaseResult(
            case_id=case.case_id,
            txn_id=case.txn_id,
            scenario=case.scenario,
            action=action,
            decided_by=decided_by,
            stages=outcome.stages,
            resolution=outcome.resolution,
            debit_txn_minor=outcome.debit_txn_minor,
            fee_usd_minor=outcome.fee_usd_minor,
            ops_usd_minor=ops,
            cost_usd_minor=cost,
            oracle_cost_usd_minor=best_cost,
            regret_usd_minor=cost - best_cost,
            amount_usd_minor=to_usd(case.amount_minor, case.dispute_currency),
        )


def _opening_ledger(cases: Sequence[DisputeCase]) -> Ledger:
    ledger = Ledger()
    seen: set[str] = set()
    for c in cases:
        if c.txn_id in seen:
            continue
        seen.add(c.txn_id)
        ledger.post(
            "capture",
            c.txn_id,
            None,
            c.txn_currency,
            [
                (f"cardholder:{c.txn_id}", -c.captured_minor),
                (f"merchant:{c.merchant_id}", c.captured_minor),
            ],
        )
        if c.refunded_minor:
            ledger.post(
                "refund",
                c.txn_id,
                None,
                c.txn_currency,
                [
                    (f"merchant:{c.merchant_id}", -c.refunded_minor),
                    (f"cardholder:{c.txn_id}", c.refunded_minor),
                ],
            )
    return ledger
