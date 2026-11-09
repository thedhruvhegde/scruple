"""Card dispute simulator: the public API for agents and tool suites.

Everything here is synthetic. Network rules come from `scruple.sim.networks` and
`scruple.sim.reason_codes` (sourced in docs/SOURCES.md); every parameter that is not
sourced is an ASSUMPTION in docs/ASSUMPTIONS.md.

Quick start::

    from scruple.sim.deadlines import ManualClock
    from scruple.sim.disputes import Accept, DisputeEngine, Represent, generate_cases

    clock = ManualClock()
    cases = generate_cases(seed=7, n=100)
    engine = DisputeEngine(cases, clock)
    obs = engine.observe(cases[0].case_id)          # what an agent may see
    res = engine.act(cases[0].case_id, Accept())    # ActionResult, never raises
    clock.advance(days=5); engine.sync()            # time moves only via the clock
    engine.outcome(cases[0].case_id)                # CaseOutcome once resolved

Pieces:

- `generate_cases(seed, n, mix)`: seeded, deterministic list of `CardDispute`.
  Ground truth sits in `CardDispute.truth` and is never part of `view()` or
  `Observation`.
- `DisputeEngine`: owns one `Lifecycle` per case (explicit transition table,
  `scruple.sim.lifecycle`), stage deadlines (`scruple.sim.deadlines`) and a
  `CardLedger`. Issuer responses, arbitration rulings and missed deadlines happen in
  `sync()`, driven by the injected clock.
- Actions an agent can take on a case (`AgentAction`): `Accept`, `Represent`
  (with evidence ids), `Escalate` (to arbitration) and the others listed in
  `ACTION_KINDS`. Each returns an `ActionResult`; a refused action changes nothing
  and carries an `ActionError`.

Extending: new generator features add optional fields to `CaseMix` (defaults keep
old seeds byte-identical) and new fields to `CardDispute`; new agent actions add a
model to `AgentAction` and a branch in `DisputeEngine._apply`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.clock import Clock
from scruple.sim.card_ledger import CardLedger
from scruple.sim.deadlines import (
    DEFAULT_CALENDAR,
    BusinessCalendar,
    Deadline,
    ManualClock,
    filing_deadline,
    stage_deadline,
    today,
)
from scruple.sim.evidence import (
    EvidenceDocument,
    EvidenceError,
    EvidenceKind,
    EvidenceVault,
    Forgery,
    forge,
    make_document,
)
from scruple.sim.fees import DEFAULT_FEES, FeeSchedule
from scruple.sim.issuer import SeededIssuer
from scruple.sim.lifecycle import OPENING_STAGES, Event, Lifecycle, Transition
from scruple.sim.merchants import MerchantProfile, generate_merchants
from scruple.sim.models import Resolution, Stage
from scruple.sim.networks import (
    Category,
    Network,
    NetworkRules,
    WindowId,
    Workflow,
    rules_for,
)
from scruple.sim.reason_codes import ReasonCodeSpec, load_catalogue, reason_code

_FROZEN = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- cases
class Truth(BaseModel):
    """Hidden ground truth. Never shown to an agent."""

    model_config = _FROZEN

    claim_valid: bool
    liability_minor: int = Field(ge=0)  # what the merchant truly owes, txn currency
    forged: dict[str, Forgery] = Field(default_factory=dict)  # doc_id -> how it was forged
    missing_evidence: tuple[EvidenceKind, ...] = ()  # required kinds lost by the merchant


class CardDispute(BaseModel):
    model_config = _FROZEN

    case_id: str
    txn_id: str
    merchant_id: str
    mcc: str
    network: Network
    reason_code: str
    currency: str
    amount_minor: int = Field(gt=0)  # disputed amount, txn currency
    captured_minor: int = Field(gt=0)
    processed_on: date  # transaction processing date
    opened_on: date
    opening_stage: Stage
    documents: tuple[EvidenceDocument, ...] = ()  # what the merchant holds
    truth: Truth

    @model_validator(mode="after")
    def _consistent(self) -> CardDispute:
        reason_code(self.network, self.reason_code)  # raises KeyError if unknown
        if self.amount_minor > self.captured_minor:
            raise ValueError("cannot dispute more than was captured")
        if self.truth.liability_minor > self.amount_minor:
            raise ValueError("liability cannot exceed the disputed amount")
        if not self.truth.claim_valid and self.truth.liability_minor:
            raise ValueError("an invalid claim carries no liability")
        if self.opening_stage not in OPENING_STAGES:
            raise ValueError(f"cannot open in {self.opening_stage}")
        if not (
            self.processed_on <= self.opened_on <= filing_deadline(self.rules, self.processed_on)
        ):
            raise ValueError("dispute opened outside the issuer filing window")
        if any(d.case_id != self.case_id for d in self.documents):
            raise ValueError("a document belongs to another case")
        if len({d.doc_id for d in self.documents}) != len(self.documents):
            raise ValueError("duplicate document id")
        if not set(self.truth.forged) <= set(self.evidence_ids):
            raise ValueError("forged ids must name documents of this case")
        return self

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(d.doc_id for d in self.documents)

    @property
    def rules(self) -> NetworkRules:
        return rules_for(self.network)

    @property
    def spec(self) -> ReasonCodeSpec:
        return reason_code(self.network, self.reason_code)

    @property
    def category(self) -> Category:
        return self.spec.category

    @property
    def workflow(self) -> Workflow:
        return rules_for(self.network).workflow(self.category)

    def view(self) -> dict[str, object]:
        """Observable fields only (no ground truth)."""
        return self.model_dump(mode="json", exclude={"truth"})


class CaseMix(BaseModel):
    """Generator knobs. New features add fields with defaults that change nothing."""

    model_config = _FROZEN

    network_weights: dict[Network, float] = Field(
        default_factory=lambda: {Network.VISA: 0.6, Network.MASTERCARD: 0.4}
    )
    p_inquiry: float = Field(0.2, ge=0, le=1)  # share opening as an inquiry
    p_claim_valid: float = Field(0.5, ge=0, le=1)
    n_merchants: int = Field(40, ge=1)
    # 024: a merchant in the right loses each required document with this probability
    p_missing_evidence: float = Field(0.0, ge=0, le=1)
    # 024: a merchant facing a valid claim forges each missing required document
    p_forged_evidence: float = Field(0.0, ge=0, le=1)


DEFAULT_MIX = CaseMix()
START_DATE = date(2026, 1, 5)
CURRENCY = "USD"


def _cnp_codes(network: Network) -> tuple[ReasonCodeSpec, ...]:
    return tuple(c for c in load_catalogue().for_network(network) if not c.card_present)


def generate_cases(
    seed: int, n: int, mix: CaseMix = DEFAULT_MIX, start: date = START_DATE
) -> list[CardDispute]:
    """Seeded synthetic disputes. Same (seed, n, mix, start) gives identical cases."""
    rng = random.Random(seed)
    merchants = generate_merchants(seed, mix.n_merchants)
    networks = sorted(mix.network_weights, key=lambda x: x.value)
    weights = [mix.network_weights[x] for x in networks]
    out: list[CardDispute] = []
    for i in range(n):
        net = rng.choices(networks, weights=weights, k=1)[0]
        spec = rng.choice(_cnp_codes(net))
        amount = max(500, int(rng.lognormvariate(4.4, 0.9) * 100))
        processed = start + timedelta(days=rng.randint(0, 60))
        opened = processed + timedelta(days=rng.randint(3, 90))
        valid = rng.random() < mix.p_claim_valid
        stage = Stage.INQUIRY if rng.random() < mix.p_inquiry else Stage.CHARGEBACK
        key = f"{seed}-{i:06d}"
        merchant = rng.choice(merchants)
        case_id = f"cd-{key}"
        docs, forged, missing = _documents(
            rng, case_id, spec, valid, processed, amount, CURRENCY, mix
        )
        out.append(
            CardDispute(
                case_id=case_id,
                txn_id=f"tx-{key}",
                merchant_id=merchant.merchant_id,
                mcc=merchant.mcc,
                network=net,
                reason_code=spec.code,
                currency=CURRENCY,
                amount_minor=amount,
                captured_minor=amount,
                processed_on=processed,
                opened_on=opened,
                opening_stage=stage,
                documents=docs,
                truth=Truth(
                    claim_valid=valid,
                    liability_minor=amount if valid else 0,
                    forged=forged,
                    missing_evidence=missing,
                ),
            )
        )
    return out


# Chance that the merchant holds each evidence kind (ASSUMPTION A-19). A merchant in
# the right usually holds what the reason code needs; one facing a valid claim rarely
# does. Every case has a receipt and an authorization record.
P_REQUIRED_HELD = {False: 1.0, True: 0.25}  # keyed by claim_valid
P_SUPPORTING_HELD = {False: 0.6, True: 0.2}
ALWAYS_HELD = (EvidenceKind.RECEIPT, EvidenceKind.AUTHORIZATION_RECORD)


def _fields(
    rng: random.Random, kind: EvidenceKind, processed: date, amount: int, currency: str
) -> tuple[date, dict[str, str]]:
    ref = f"{rng.getrandbits(48):012x}"
    f = {"ref": ref, "amount_minor": str(amount), "currency": currency}
    issued = processed
    match kind:
        case EvidenceKind.DELIVERY_PROOF:
            issued = processed + timedelta(days=rng.randint(1, 10))
            f |= {"carrier": rng.choice(("ups", "usps", "dhl")), "delivered_on": issued.isoformat()}
        case EvidenceKind.AUTHORIZATION_RECORD:
            f |= {"auth_code": f"{rng.randint(0, 999_999):06d}", "approved": "Y"}
        case EvidenceKind.AVS_CVV_MATCH:
            f |= {"avs": "Y", "cvv": "M"}
        case EvidenceKind.DEVICE_IP_MATCH:
            f |= {"ip": f"203.0.113.{rng.randint(1, 254)}", "device_id": f"dev-{ref}"}
        case EvidenceKind.REFUND_RECORD:
            issued = processed + timedelta(days=rng.randint(1, 20))
            f |= {"refund_id": f"rf-{ref}", "settled_on": issued.isoformat()}
        case _:
            pass
    return issued, f


def _documents(
    rng: random.Random,
    case_id: str,
    spec: ReasonCodeSpec,
    valid: bool,
    processed: date,
    amount: int,
    currency: str,
    mix: CaseMix = DEFAULT_MIX,
) -> tuple[tuple[EvidenceDocument, ...], dict[str, Forgery], tuple[EvidenceKind, ...]]:
    """(documents held, forged doc ids, required kinds lost). Draws extra randomness
    only when the 024 options are non-zero, so default seeds are unchanged."""
    kinds: list[EvidenceKind] = list(ALWAYS_HELD)
    for k in spec.required_evidence:
        if k not in kinds and rng.random() < P_REQUIRED_HELD[valid]:
            kinds.append(k)
    for k in spec.supporting_evidence:
        if k not in kinds and rng.random() < P_SUPPORTING_HELD[valid]:
            kinds.append(k)
    missing: list[EvidenceKind] = []
    if mix.p_missing_evidence > 0 and not valid:
        for k in spec.required_evidence:
            if k in kinds and k not in ALWAYS_HELD and rng.random() < mix.p_missing_evidence:
                kinds.remove(k)
                missing.append(k)
    to_forge: list[EvidenceKind] = []
    if mix.p_forged_evidence > 0 and valid:
        for k in spec.required_evidence:
            if k not in kinds and rng.random() < mix.p_forged_evidence:
                kinds.append(k)
                to_forge.append(k)
    docs: list[EvidenceDocument] = []
    forged: dict[str, Forgery] = {}
    for idx, k in enumerate(kinds):
        issued, fields = _fields(rng, k, processed, amount, currency)
        d = make_document(case_id, k, issued, fields, idx)
        if k in to_forge:
            how = rng.choice(tuple(Forgery))
            d = forge(d, how, processed)
            forged[d.doc_id] = how
        docs.append(d)
    return tuple(docs), forged, tuple(missing)


# ------------------------------------------------------------------------- actions
class Accept(BaseModel):
    """Accept liability: the chargeback stands."""

    model_config = _FROZEN
    kind: Literal["accept"] = "accept"


class Represent(BaseModel):
    """Respond to an inquiry or chargeback with evidence."""

    model_config = _FROZEN
    kind: Literal["represent"] = "represent"
    evidence_ids: tuple[str, ...] = ()


class Escalate(BaseModel):
    """Take a pre-arbitration case to network arbitration."""

    model_config = _FROZEN
    kind: Literal["escalate"] = "escalate"


AgentAction = Annotated[Accept | Represent | Escalate, Field(discriminator="kind")]
ACTION_KINDS: tuple[str, ...] = ("accept", "represent", "escalate")


class ActionError(StrEnum):
    UNKNOWN_CASE = "unknown_case"
    NOT_OPEN = "not_open"
    RESOLVED = "resolved"
    NOT_ALLOWED = "not_allowed"
    AWAITING_OTHER_PARTY = "awaiting_other_party"
    BAD_EVIDENCE = "bad_evidence"  # unknown, repeated, or another case's evidence id


Awaiting = Literal["merchant", "issuer", "network", "nobody"]


class ActionResult(BaseModel):
    model_config = _FROZEN

    case_id: str
    action: str
    ok: bool
    error: ActionError | None = None
    detail: str = ""
    stage_before: Stage | None = None
    stage_after: Stage | None = None
    entries: tuple[int, ...] = ()  # journal entry ids posted by this action


class Observation(BaseModel):
    """Everything an agent may see about one case."""

    model_config = _FROZEN

    case: dict[str, object]
    today: date
    stage: Stage
    awaiting: Awaiting
    deadline: Deadline | None
    allowed_actions: tuple[str, ...]
    history: tuple[Transition, ...]
    merchant: MerchantProfile | None = None


class CaseOutcome(BaseModel):
    model_config = _FROZEN

    case_id: str
    resolution: Resolution
    path: tuple[Stage, ...]
    resolved_on: date
    cardholder_net_minor: int  # what the cardholder ended up with, txn currency
    fees_usd_minor: int
    liability_minor: int  # ground truth, for scoring only
    correct: bool  # cardholder_net == liability


# -------------------------------------------------------------------------- issuer
class Issuer(Protocol):
    """Decides the other side of the dispute. Must be deterministic."""

    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        """At INQUIRY, REPRESENTMENT, PRE_ARBITRATION (allocation) and ARBITRATION."""
        ...


class TruthIssuer:
    """Oracle issuer for tests: the merchant prevails exactly when the claim is invalid.

    The default is `scruple.sim.issuer.SeededIssuer`, which judges evidence only."""

    def merchant_prevails(
        self, case: CardDispute, stage: Stage, evidence: tuple[EvidenceDocument, ...]
    ) -> bool:
        return not case.truth.claim_valid


@dataclass(frozen=True)
class EngineParams:
    """Timing parameters (ASSUMPTION A-25)."""

    issuer_response_days: int = 10
    arbitration_ruling_days: int = 30


# -------------------------------------------------------------------------- engine
@dataclass
class _CaseState:
    case: CardDispute
    life: Lifecycle
    awaiting: Awaiting
    due: date | None = None  # when the issuer or network acts
    declined_on: date | None = None  # allocation: issuer declined the pre-arbitration
    evidence: tuple[EvidenceDocument, ...] = ()
    resolution: Resolution | None = None
    entry_ids: list[int] = field(default_factory=list)


class DisputeEngine:
    def __init__(
        self,
        cases: list[CardDispute],
        clock: Clock,
        *,
        fees: dict[Network, FeeSchedule] | None = None,
        issuer: Issuer | None = None,
        merchants: tuple[MerchantProfile, ...] = (),
        params: EngineParams | None = None,
        calendar: BusinessCalendar = DEFAULT_CALENDAR,
    ) -> None:
        self.clock = clock
        self.fees = fees if fees is not None else DEFAULT_FEES
        self.issuer: Issuer = issuer if issuer is not None else SeededIssuer()
        self.params = params or EngineParams()
        self.calendar = calendar
        self.merchants = {m.merchant_id: m for m in merchants}
        self.vault = EvidenceVault(d for c in cases for d in c.documents)
        self.ledger = CardLedger()
        self._cases: dict[str, _CaseState] = {}
        for c in cases:
            if c.case_id in self._cases:
                raise ValueError(f"duplicate case id {c.case_id}")
            st = _CaseState(
                case=c,
                life=Lifecycle(c.workflow, c.opened_on, c.opening_stage),
                awaiting="merchant",
            )
            self._cases[c.case_id] = st
            self.ledger.capture(c.txn_id, c.merchant_id, c.captured_minor, c.currency)
            if c.opening_stage is Stage.CHARGEBACK:
                self._on_chargeback(st)

    # --- queries ------------------------------------------------------------
    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._cases)

    def case(self, case_id: str) -> CardDispute:
        return self._cases[case_id].case

    def is_resolved(self, case_id: str) -> bool:
        return self._cases[case_id].resolution is not None

    def open_cases(self) -> tuple[str, ...]:
        return tuple(k for k, s in self._cases.items() if s.resolution is None)

    def deadline(self, case_id: str) -> Deadline | None:
        st = self._cases[case_id]
        if st.awaiting != "merchant":
            return None
        if st.declined_on is not None:
            w = st.case.rules.window(WindowId.ARBITRATION_FILING)
            due = st.declined_on + timedelta(days=w.days)
            return Deadline(
                stage=st.life.stage,
                window=w.window,
                actor="merchant",
                starts=st.declined_on,
                due=due,
                day_kind=w.day_kind,
            )
        return stage_deadline(st.case.rules, st.life.stage, st.life.entered_on, self.calendar)

    def allowed_actions(self, case_id: str) -> tuple[str, ...]:
        st = self._cases[case_id]
        if st.resolution is not None or st.awaiting != "merchant":
            return ()
        stage = st.life.stage
        out: list[str] = []
        if stage in (Stage.CHARGEBACK, Stage.PRE_ARBITRATION):
            out.append("accept")
        if stage in (Stage.INQUIRY, Stage.CHARGEBACK):
            out.append("represent")
        if stage is Stage.PRE_ARBITRATION:
            out.append("escalate")
        return tuple(out)

    def observe(self, case_id: str) -> Observation:
        self.sync()
        st = self._cases[case_id]
        return Observation(
            case=st.case.view(),
            today=today(self.clock),
            stage=st.life.stage,
            awaiting="nobody" if st.resolution is not None else st.awaiting,
            deadline=self.deadline(case_id),
            allowed_actions=self.allowed_actions(case_id),
            history=st.life.history,
            merchant=self.merchants.get(st.case.merchant_id),
        )

    def outcome(self, case_id: str) -> CaseOutcome | None:
        st = self._cases[case_id]
        if st.resolution is None:
            return None
        c = st.case
        net = self.ledger.returned(c.txn_id)
        fees = sum(
            -p.amount_minor
            for e in self.ledger.entries
            if e.case_id == c.case_id and e.kind.startswith("fee:")
            for p in e.postings
            if p.account.startswith("merchant:")
        )
        return CaseOutcome(
            case_id=c.case_id,
            resolution=st.resolution,
            path=st.life.path,
            resolved_on=st.life.entered_on,
            cardholder_net_minor=net,
            fees_usd_minor=fees,
            liability_minor=c.truth.liability_minor,
            correct=net == c.truth.liability_minor,
        )

    # --- actions ------------------------------------------------------------
    def act(self, case_id: str, action: AgentAction) -> ActionResult:
        """Apply one agent action. Never raises for a bad action: returns ok=False."""
        if case_id not in self._cases:
            return ActionResult(
                case_id=case_id, action=action.kind, ok=False, error=ActionError.UNKNOWN_CASE
            )
        self.sync()
        st = self._cases[case_id]
        now = today(self.clock)
        before = st.life.stage

        def refuse(err: ActionError, detail: str = "") -> ActionResult:
            return ActionResult(
                case_id=case_id,
                action=action.kind,
                ok=False,
                error=err,
                detail=detail,
                stage_before=before,
                stage_after=before,
            )

        if now < st.case.opened_on:
            return refuse(ActionError.NOT_OPEN, f"opens on {st.case.opened_on}")
        if st.resolution is not None:
            return refuse(ActionError.RESOLVED)
        if st.awaiting != "merchant":
            return refuse(ActionError.AWAITING_OTHER_PARTY, f"awaiting {st.awaiting}")
        if action.kind not in self.allowed_actions(case_id):
            return refuse(ActionError.NOT_ALLOWED, f"{action.kind} in {before.value}")
        if isinstance(action, Represent):
            try:
                self.vault.resolve(case_id, action.evidence_ids)
            except EvidenceError as exc:
                return refuse(ActionError.BAD_EVIDENCE, str(exc))
        mark = len(self.ledger.entries)
        self._apply(st, action, now)
        posted = tuple(e.entry_id for e in self.ledger.entries[mark:])
        return ActionResult(
            case_id=case_id,
            action=action.kind,
            ok=True,
            stage_before=before,
            stage_after=st.life.stage,
            entries=posted,
        )

    def _apply(self, st: _CaseState, action: AgentAction, now: date) -> None:
        match action:
            case Accept():
                self._resolve(st, Event.ACCEPT, now, Resolution.ACCEPTED)
            case Represent(evidence_ids=ids):
                st.evidence = self.vault.resolve(st.case.case_id, ids)
                if st.life.stage is Stage.INQUIRY:
                    if self.issuer.merchant_prevails(st.case, Stage.INQUIRY, st.evidence):
                        self._resolve(st, Event.RESOLVE_INQUIRY, now, Resolution.WON)
                    else:
                        st.life.fire(Event.FILE_CHARGEBACK, now)
                        self._on_chargeback(st)
                    return
                to = st.life.fire(Event.REPRESENT, now)
                self._enter(st, to, now)
                st.awaiting = "issuer"
                st.due = now + timedelta(days=self.params.issuer_response_days)
            case Escalate():
                st.life.fire(Event.ESCALATE, now)
                self._enter(st, Stage.ARBITRATION, now)
                st.awaiting = "network"
                st.due = now + timedelta(days=self.params.arbitration_ruling_days)

    # --- time ---------------------------------------------------------------
    def sync(self) -> None:
        """Apply every issuer response, ruling and missed deadline due by today."""
        now = today(self.clock)
        for st in self._cases.values():
            while st.resolution is None and self._step_due(st, now):
                pass

    def _step_due(self, st: _CaseState, now: date) -> bool:
        if st.awaiting == "merchant":
            dl = self.deadline(st.case.case_id)
            if dl is None or now <= dl.due:
                return False
            self._missed(st, dl.due + timedelta(days=1))
            return True
        assert st.due is not None
        if now < st.due:
            return False
        day, st.due = st.due, None
        if st.awaiting == "network":
            if self.issuer.merchant_prevails(st.case, Stage.ARBITRATION, st.evidence):
                self._resolve(st, Event.RULING, day, Resolution.WON)
            else:
                self._resolve(st, Event.RULING, day, Resolution.LOST)
                self._fee(st, "fee:arbitration_loss", self._schedule(st).arbitration_loss)
            return True
        stage = st.life.stage
        prevails = self.issuer.merchant_prevails(st.case, stage, st.evidence)
        if prevails:
            self._resolve(st, Event.ISSUER_ACCEPT, day, Resolution.WON)
        elif stage is Stage.REPRESENTMENT:
            st.life.fire(Event.ISSUER_PRE_ARB, day)
            self._enter(st, Stage.PRE_ARBITRATION, day)
            st.awaiting = "merchant"
        else:  # allocation pre-arbitration declined by the issuer
            st.declined_on = day
            st.awaiting = "merchant"
        return True

    def _missed(self, st: _CaseState, day: date) -> None:
        if st.life.stage is Stage.INQUIRY:
            st.life.fire(Event.DEADLINE_MISSED, day)
            self._on_chargeback(st)
        else:
            self._resolve(st, Event.DEADLINE_MISSED, day, Resolution.LOST)

    # --- money --------------------------------------------------------------
    def _schedule(self, st: _CaseState) -> FeeSchedule:
        return self.fees[st.case.network]

    def _fee(self, st: _CaseState, kind: str, amount: int) -> None:
        payee = "processor:fees" if kind == "fee:chargeback" else "network:fees"
        self.ledger.fee(kind, st.case.txn_id, st.case.case_id, amount, payee)

    def _enter(self, st: _CaseState, stage: Stage, day: date) -> None:
        self._fee(st, f"fee:{stage.value}", self._schedule(st).entry_fee(stage))

    def _on_chargeback(self, st: _CaseState) -> None:
        c = st.case
        self.ledger.to_cardholder("chargeback_debit", c.txn_id, c.case_id, c.amount_minor)
        self._enter(st, Stage.CHARGEBACK, st.life.entered_on)
        st.awaiting = "merchant"

    def _resolve(self, st: _CaseState, event: Event, day: date, res: Resolution) -> None:
        was = st.life.stage
        st.life.fire(event, day)
        st.awaiting = "nobody"
        st.resolution = res
        if res is Resolution.WON and was is not Stage.INQUIRY:
            c = st.case
            self.ledger.to_merchant("chargeback_reversal", c.txn_id, c.case_id, c.amount_minor)


def drain(engine: DisputeEngine, clock: ManualClock, max_days: int = 800) -> None:
    """Advance the clock one day at a time until every case resolves (merchant idle)."""
    for _ in range(max_days):
        engine.sync()
        if not engine.open_cases():
            return
        clock.advance(days=1)
    raise RuntimeError("cases still open after max_days")  # pragma: no cover
