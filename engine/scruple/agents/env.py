"""Thin adapter between agent episodes and the dispute simulator.

Everything an episode needs from the simulator goes through `SimEnvironment` and the
`SimCase` it loads. A richer simulator (lifecycle stages, reason codes, evidence
documents) can be plugged in by providing another `CaseEnvironment` whose cases
implement the same methods; nothing in `scruple.agents.episode` or the runners
imports `scruple.sim` directly.

Security control: the only case features that ever leave this module are the ones in
`scruple.domain.FIELD_TYPES`, the same whitelist that learned rules are restricted to.
Ground truth (`claim_valid`), the scenario label and the case, transaction and
merchant identifiers stay inside. Evidence documents and the ledger view are built
from whitelisted features and anonymised journal postings only.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from scruple.domain import FIELD_TYPES, Action
from scruple.sim.engine import RunResult, Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.library import baseline_policy
from scruple.sim.models import DisputeCase, Scenario
from scruple.sim.policy import Policy

FieldValue = bool | int | str

# Case selection scans a seeded stream of generated cases for the requested type.
_FIRST_SCAN = 64
_MAX_SCAN = 8_192


class ActionKind(StrEnum):
    """What an agent can do with a dispute. Exactly one per episode."""

    ACCEPT_LIABILITY = "accept_liability"
    REFUND = "refund"
    REPRESENT = "represent"
    ESCALATE = "escalate"


# ADAPTER: the current simulator has three actions. A refund is modelled as accepting
# liability (the cardholder gets the chargeback amount back); a richer simulator can
# give refunds their own path (for example, refunding at the inquiry stage).
SIM_ACTION: dict[ActionKind, Action] = {
    ActionKind.ACCEPT_LIABILITY: Action.ACCEPT,
    ActionKind.REFUND: Action.ACCEPT,
    ActionKind.REPRESENT: Action.CONTEST,
    ActionKind.ESCALATE: Action.ESCALATE,
}


class GroundTruth(BaseModel):
    """Hidden labels. Only scoring and the oracle baseline ever see these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_type: str
    claim_valid: bool


class EvidenceDoc(BaseModel):
    """A synthetic evidence document. Its body is built from whitelisted fields only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    kind: str
    title: str
    body: dict[str, FieldValue]


class LedgerPosting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account: str  # anonymised: merchant | cardholder | network_fees
    amount_minor: int


class LedgerEntryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: int
    kind: str  # capture | refund | chargeback_debit | fee
    currency: str
    earlier_dispute: bool  # posted for an earlier dispute on the same transaction
    postings: tuple[LedgerPosting, ...]


class LedgerView(BaseModel):
    """A read-only copy of the journal for this transaction. Mutating it changes
    nothing: every call builds a fresh copy from the simulator's journal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[LedgerEntryView, ...]
    balances: dict[str, int]  # "account currency" -> balance, minor units
    read_only: bool = True


class InfoResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str
    new_evidence_ids: tuple[str, ...] = ()


def _anon(account: str) -> str:
    if account.startswith("merchant:"):
        return "merchant"
    if account.startswith("cardholder:"):
        return "cardholder"
    return account.replace(":", "_")


def evidence_for(view: dict[str, FieldValue]) -> tuple[EvidenceDoc, ...]:
    """Evidence documents as a pure function of the whitelisted view."""
    docs: list[tuple[str, str, dict[str, FieldValue]]] = [
        (
            "authorization",
            "Authorization record",
            {"three_ds_authenticated": view["three_ds"], "mcc": view["mcc"]},
        )
    ]
    if view["delivery_confirmed"]:
        docs.append(("delivery", "Carrier delivery confirmation", {"delivered": True}))
    if view["has_prior_refund"]:
        docs.append(("refund_record", "Earlier partial refund", {"prior_refund_issued": True}))
    if view["prior_dispute_on_txn"]:
        docs.append(
            (
                "prior_dispute",
                "Earlier dispute on the same transaction",
                {"earlier_dispute_on_same_transaction": True},
            )
        )
    if view["currency_mismatch"]:
        docs.append(
            (
                "fx_record",
                "Settlement and dispute currencies",
                {"dispute_and_settlement_currency_differ": True},
            )
        )
    docs.append(
        (
            "merchant_statement",
            "Merchant response packet",
            {
                "reason_code": view["reason_code"],
                "ready_before_response_deadline": view["evidence_on_time"],
            },
        )
    )
    return tuple(
        EvidenceDoc(evidence_id=f"ev-{i:02d}", kind=k, title=t, body=b)
        for i, (k, t, b) in enumerate(docs, start=1)
    )


def observable(case: DisputeCase) -> dict[str, FieldValue]:
    """The whitelisted view of a case, and nothing else."""
    view = case.view()
    out = {k: view[k] for k in sorted(FIELD_TYPES)}
    if set(out) != set(FIELD_TYPES):  # pragma: no cover - guarded by domain.py too
        raise AssertionError("observation does not match the field whitelist")
    return out


class SimCase:
    """One dispute, plus any earlier disputes on the same transaction (history).

    History is resolved with the synthetic baseline policy when the case is loaded,
    so the ledger already shows what happened to earlier disputes. The agent's action
    then resolves the target case on the same ledger.
    """

    def __init__(self, history: Sequence[DisputeCase], target: DisputeCase) -> None:
        self._target = target
        self._cases = (*history, target)
        self._sim = Simulator(self._cases)
        base = baseline_policy()
        for _ in history:
            self._sim.step(base)
        self._action: Action | None = None

    # --- what the agent may see -------------------------------------------------

    def observable(self) -> dict[str, FieldValue]:
        return observable(self._target)

    def evidence(self) -> tuple[EvidenceDoc, ...]:
        return evidence_for(self.observable())

    def ledger_view(self) -> LedgerView:
        target = self._target.case_id
        entries: list[LedgerEntryView] = []
        balances: dict[str, int] = {}
        for e in self._sim.result().journal:
            postings = tuple(
                LedgerPosting(account=_anon(p.account), amount_minor=p.amount_minor)
                for p in e.postings
            )
            for p in postings:
                key = f"{p.account} {e.currency}"
                balances[key] = balances.get(key, 0) + p.amount_minor
            entries.append(
                LedgerEntryView(
                    entry=e.entry_id,
                    kind=e.kind,
                    currency=e.currency,
                    earlier_dispute=e.case_id is not None and e.case_id != target,
                    postings=postings,
                )
            )
        return LedgerView(entries=tuple(entries), balances=dict(sorted(balances.items())))

    def request_info(self, question: str) -> InfoResponse:
        # ADAPTER: the current simulator has nothing beyond the evidence list. A richer
        # simulator can answer from cardholder or issuer messages here.
        del question
        return InfoResponse(answer="No further information is available for this case.")

    # --- what the agent may do --------------------------------------------------

    @property
    def resolved(self) -> bool:
        return self._action is not None

    def resolve(self, kind: ActionKind) -> Action:
        if self._action is not None:
            raise RuntimeError("case already resolved")
        action = SIM_ACTION[kind]
        self._sim.step(Policy(rules=(), default_action=action))
        self._action = action
        return action

    # --- scoring only (never handed to an agent) ----------------------------------

    @property
    def cases(self) -> tuple[DisputeCase, ...]:
        return self._cases

    @property
    def target_id(self) -> str:
        return self._target.case_id

    def run(self) -> RunResult:
        return self._sim.result()

    def ground_truth(self) -> GroundTruth:
        return GroundTruth(
            case_type=self._target.scenario.value, claim_valid=self._target.claim_valid
        )


class CaseEnvironment(Protocol):
    """What an episode needs from a simulator. `SimEnvironment` is the default."""

    name: str

    def case_types(self) -> tuple[str, ...]: ...

    def load(self, seed: int, case_type: str | None) -> SimCase: ...


class SimEnvironment:
    """The current deterministic dispute simulator (8 synthetic case types)."""

    name = "scruple.sim"

    def case_types(self) -> tuple[str, ...]:
        return tuple(sorted(s.value for s in Scenario))

    def load(self, seed: int, case_type: str | None) -> SimCase:
        if case_type is not None and case_type not in self.case_types():
            raise ValueError(
                f"unknown case type {case_type!r}; choose from {', '.join(self.case_types())}"
            )
        n = _FIRST_SCAN
        while True:
            cases = generate_cases(seed, n, prefix="e")
            for i, c in enumerate(cases):
                if case_type is None or c.scenario.value == case_type:
                    history = [h for h in cases[:i] if h.txn_id == c.txn_id]
                    return SimCase(history, c)
            if n >= _MAX_SCAN:  # pragma: no cover - every type has weight > 0
                raise ValueError(f"no {case_type} case in the first {n} cases of seed {seed}")
            n *= 2
