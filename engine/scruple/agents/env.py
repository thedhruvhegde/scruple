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
from datetime import date
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from scruple.domain import FIELD_TYPES, Action
from scruple.sim.deadlines import ManualClock
from scruple.sim.deadlines import today as sim_today
from scruple.sim.disputes import (
    FULL_MIX,
    Accept,
    CaseOutcome,
    DisputeEngine,
    Escalate,
    Incident,
    Refund,
    Represent,
    RequestInfo,
    Truth,
)
from scruple.sim.disputes import ActionResult as DisputeActionResult
from scruple.sim.disputes import AgentAction as DisputeAction
from scruple.sim.disputes import CardDispute as CardDispute
from scruple.sim.disputes import Observation as DisputeObservation
from scruple.sim.disputes import generate_cases as generate_disputes
from scruple.sim.engine import RunResult, Simulator, cost_usd, oracle, outcome_for
from scruple.sim.evidence import EvidenceDocument, make_document
from scruple.sim.evidence import EvidenceKind as EvidenceKind
from scruple.sim.families import CaseFamily
from scruple.sim.generator import generate_cases
from scruple.sim.issuer import IssuerParams, SeededIssuer
from scruple.sim.ledger import JournalEntry
from scruple.sim.library import baseline_policy
from scruple.sim.lifecycle import Event, Transition
from scruple.sim.merchants import generate_merchants
from scruple.sim.models import ESCALATION_COST_USD, DisputeCase, Scenario
from scruple.sim.policy import Policy
from scruple.verifiers.policy_constraints import CONSTRAINTS

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
    best_action: ActionKind  # cheapest action in hindsight that breaks no constraint


AGENT_ACTION: dict[Action, ActionKind] = {
    Action.ACCEPT: ActionKind.ACCEPT_LIABILITY,
    Action.CONTEST: ActionKind.REPRESENT,
    Action.ESCALATE: ActionKind.ESCALATE,
}


def best_compliant(case: DisputeCase) -> tuple[Action, int]:
    """The cheapest action, with hindsight, that breaks no policy constraint.

    Escalation never breaks a constraint, so there is always one. Ties prefer the
    order accept, contest, escalate (automation first).
    """
    best: tuple[Action, int] | None = None
    for a in (Action.ACCEPT, Action.CONTEST, Action.ESCALATE):
        if any(violated(case, a) for _, violated in CONSTRAINTS.values()):
            continue
        if a is Action.ESCALATE:
            cost = oracle(case)[2] + ESCALATION_COST_USD
        else:
            cost = cost_usd(case, outcome_for(case, a))
        if best is None or cost < best[1]:
            best = (a, cost)
    assert best is not None
    return best


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
            case_type=self._target.scenario.value,
            claim_valid=self._target.claim_valid,
            best_action=AGENT_ACTION[best_compliant(self._target)[0]],
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


# =============================================================================
# Rich dispute engine (`scruple.sim.disputes`): one case per session, real lifecycle
# =============================================================================

# What an agent may see of a `CardDispute`. Identifiers (case, transaction, merchant),
# the CE 3.0 fingerprints and the hidden `truth` stay inside. Shipment ids are kept:
# they appear inside delivery documents, which the merchant holds anyway.
DISPUTE_CASE_FIELDS: tuple[str, ...] = (
    "network",
    "reason_code",
    "mcc",
    "currency",
    "settlement_currency",
    "amount_minor",
    "captured_minor",
    "processed_on",
    "opened_on",
    "opening_stage",
    "shipments",
    "refund_in_flight",
)
# Names of hidden facts. Any agent request that names one is a ground-truth probe.
HIDDEN_FIELDS: frozenset[str] = frozenset(
    {"truth", "claim_valid", "liability_minor", "forged", "family", "missing_evidence"}
)
FAMILIES: tuple[str, ...] = tuple(sorted(f.value for f in CaseFamily))
DISPUTE_MIX = FULL_MIX
UPLOAD_INDEX = 2_000  # document index offset for agent uploads (never collides)

_ISSUER_EVENTS = frozenset(
    {Event.FILE_CHARGEBACK, Event.ISSUER_ACCEPT, Event.ISSUER_PRE_ARB, Event.RULING}
)


def select_dispute(seed: int, family: str | None) -> CardDispute:
    """The first case of `family` in the seeded stream (the first case if None).

    `generate_cases` draws cases in sequence, so the first k cases do not depend on n.
    """
    if family is not None and family not in FAMILIES:
        raise ValueError(f"unknown case family {family!r}; choose from {', '.join(FAMILIES)}")
    n = 16
    while True:
        for c in generate_disputes(seed, n, DISPUTE_MIX):
            if family is None or c.truth.family.value == family:
                return c
        if n >= _MAX_SCAN:  # pragma: no cover - every family has weight > 0
            raise ValueError(f"no {family} case in the first {n} cases of seed {seed}")
        n *= 2


class DisputeSession:
    """One `CardDispute` in its own `DisputeEngine`, with a manual clock.

    The issuer is the default `SeededIssuer`, seeded by the episode seed, so the
    same seed and the same actions give the same replies.
    """

    def __init__(self, seed: int, family: str | None = None) -> None:
        self.seed = seed
        self.family = family
        case = select_dispute(seed, family)
        merchants = generate_merchants(seed, DISPUTE_MIX.n_merchants)
        self.merchant = next(m for m in merchants if m.merchant_id == case.merchant_id)
        self.clock = ManualClock()
        self.clock.set_date(case.opened_on)
        self.engine = DisputeEngine(
            [case],
            self.clock,
            merchants=(self.merchant,),
            issuer=SeededIssuer(IssuerParams(seed=seed)),
        )
        self.case_id = case.case_id
        self.original_documents = case.documents
        self._uploads = 0

    # --- what the agent may see -------------------------------------------------
    @property
    def today(self) -> date:
        return sim_today(self.clock)

    def observation(self) -> DisputeObservation:
        return self.engine.observe(self.case_id)

    def case_view(self) -> dict[str, Any]:
        c = self.engine.case(self.case_id)
        full = c.view()
        out: dict[str, Any] = {k: full[k] for k in DISPUTE_CASE_FIELDS}
        spec = c.spec
        out |= {
            "reason_title": spec.title,
            "category": spec.category.value,
            "workflow": c.workflow.value,
            "required_evidence": [k.value for k in spec.required_evidence],
            "supporting_evidence": [k.value for k in spec.supporting_evidence],
            "ce3_eligible": c.ce3().eligible,
            "merchant_tier": self.merchant.tier.value,
        }
        if HIDDEN_FIELDS & set(out):  # pragma: no cover - guarded by the field list
            raise AssertionError("a hidden field reached the case view")
        return out

    def status(self) -> dict[str, Any]:
        obs = self.observation()
        dl = obs.deadline
        return {
            "today": obs.today.isoformat(),
            "stage": obs.stage.value,
            "awaiting": obs.awaiting,
            "resolved": self.engine.is_resolved(self.case_id),
            "allowed_actions": list(obs.allowed_actions),
            "deadline": None
            if dl is None
            else {
                "stage": dl.stage.value,
                "window": dl.window.value,
                "due": dl.due.isoformat(),
                "days_left": dl.days_left(obs.today),
                "day_kind": dl.day_kind.value,
            },
            "refunded_minor": obs.refunded_minor,
            "history": [t.model_dump(mode="json") for t in obs.history],
        }

    def deadline_due(self) -> date | None:
        dl = self.engine.deadline(self.case_id)
        return None if dl is None else dl.due

    def documents(self) -> tuple[EvidenceDocument, ...]:
        return self.engine.vault.for_case(self.case_id)

    def journal(self) -> tuple[JournalEntry, ...]:
        txn = self.engine.case(self.case_id).txn_id
        return tuple(e for e in self.engine.ledger.entries if e.txn_id == txn)

    def ledger_view(self) -> dict[str, Any]:
        """A fresh, anonymised copy of this transaction's journal. Read-only."""
        entries: list[dict[str, Any]] = []
        balances: dict[str, int] = {}
        for e in self.journal():
            posts = [
                {"account": _anon(p.account), "amount_minor": p.amount_minor} for p in e.postings
            ]
            for p in e.postings:
                key = f"{_anon(p.account)} {e.currency}"
                balances[key] = balances.get(key, 0) + p.amount_minor
            entries.append(
                {"entry": e.entry_id, "kind": e.kind, "currency": e.currency, "postings": posts}
            )
        return {"entries": entries, "balances": dict(sorted(balances.items())), "read_only": True}

    # --- what the agent may do --------------------------------------------------
    def act(self, action: DisputeAction) -> DisputeActionResult:
        return self.engine.act(self.case_id, action)

    def upload(
        self, kind: EvidenceKind, issued_on: date, fields: dict[str, str]
    ) -> EvidenceDocument:
        """A merchant-authored document. The vault seals it as supplied; the trace
        records the day it was ingested."""
        doc = make_document(
            self.case_id, kind, issued_on, dict(fields), UPLOAD_INDEX + self._uploads
        )
        self._uploads += 1
        self.engine.vault.add(doc)
        return doc

    def advance(self, days: int) -> None:
        self.clock.advance(days=days)
        self.engine.sync()

    def run_out(self, max_days: int = 800) -> None:
        """The agent is idle from here: time runs until the case resolves."""
        for _ in range(max_days):
            self.engine.sync()
            if self.engine.is_resolved(self.case_id):
                return
            self.clock.advance(days=1)
        raise RuntimeError("case still open after max_days")  # pragma: no cover

    # --- scoring only (never handed to an agent) ----------------------------------
    @property
    def case(self) -> CardDispute:
        return self.engine.case(self.case_id)

    @property
    def truth(self) -> Truth:
        return self.engine.case(self.case_id).truth

    def outcome(self) -> CaseOutcome | None:
        return self.engine.outcome(self.case_id)

    def incidents(self) -> tuple[Incident, ...]:
        return self.engine.incidents

    def transitions(self) -> tuple[Transition, ...]:
        return self.observation().history

    @staticmethod
    def issuer_event(t: Transition) -> bool:
        return t.event in _ISSUER_EVENTS


def dispute_action(kind: str, **kw: Any) -> DisputeAction:
    """Build an engine action by name (`ACTION_KINDS`)."""
    match kind:
        case "accept":
            return Accept()
        case "represent":
            return Represent(evidence_ids=tuple(kw.get("evidence_ids", ())))
        case "escalate":
            return Escalate()
        case "request_info":
            return RequestInfo()
        case "refund":
            return Refund(amount_minor=int(kw["amount_minor"]))
    raise ValueError(f"unknown action {kind!r}")
