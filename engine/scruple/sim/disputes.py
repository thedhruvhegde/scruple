"""Card dispute simulator: the public API for agents and tool suites.

Everything here is synthetic. Network rules come from `scruple.sim.networks` and
`scruple.sim.reason_codes` (sourced in docs/SOURCES.md); every parameter that is not
sourced is an ASSUMPTION in docs/ASSUMPTIONS.md (A-13 onwards).

Quick start::

    from scruple.sim.deadlines import ManualClock
    from scruple.sim.disputes import FULL_MIX, Accept, DisputeEngine, generate_cases

    cases = generate_cases(seed=7, n=100, mix=FULL_MIX)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    engine = DisputeEngine(cases, clock)
    cid = cases[0].case_id
    obs = engine.observe(cid)        # Observation: what an agent may see
    res = engine.act(cid, Accept())  # ActionResult; never raises for a bad action
    clock.advance(days=5)            # time moves only through the injected clock
    engine.sync()                    # issuer replies, rulings, deadlines, refunds
    engine.outcome(cid)              # CaseOutcome once resolved (scored vs truth)

Generation: `generate_cases(seed, n, mix)` returns `CardDispute`s, deterministic in
(seed, n, mix, start). Ground truth lives in `CardDispute.truth` and never appears in
`view()` or `Observation`. `CaseMix` options, all off by default except the base mix:

- `family_weights` (026): true fraud, friendly fraud, service failure, ...
- `p_missing_evidence`, `p_forged_evidence` (024)
- `p_refund_race` (027), `p_split_shipment`, `p_partial_dispute` (028)
- `p_foreign_currency` (029), `p_ce3_history` (030)
- `p_required_held_*`, `p_supporting_held_*` (032): evidence availability
`FULL_MIX` turns every feature on at moderate rates. `scruple.sim.difficulty.
difficulty_mix(d)` maps one number in [0, 1] to a mix (0.5 is exactly `FULL_MIX`).

Actions (`AgentAction`, a pydantic union discriminated on `kind`; `ACTION_KINDS`):

- `Accept()`: accept liability (chargeback or pre-arbitration).
- `Represent(evidence_ids=...)`: answer an inquiry or a chargeback with evidence ids
  from `Observation.evidence`. Ids of another case are refused (`BAD_EVIDENCE`).
- `Escalate()`: take a pre-arbitration to network arbitration.
- `RequestInfo()`: the cardholder's statement, in `ActionResult.info`. No state change.
- `Refund(amount_minor=...)`: refund directly; refused if it would refund twice.

`Observation.allowed_actions` lists what is legal now. A refused action returns
`ok=False` with an `ActionError` and changes nothing. Underneath, every stage change
goes through `scruple.sim.lifecycle.Lifecycle`, whose explicit transition table raises
on anything illegal.

Money: `DisputeEngine.ledger` is a `CardLedger` (double entry, settlement currency,
fees in USD). `engine.incidents` lists double refunds the ledger caught (027).
`engine.post_monitoring_fines()` applies VAMP / ECM fines from merchant histories
(031). `CardDispute.ce3()` reports Visa CE 3.0 eligibility (030).

Events (036): every state change is an `EngineEvent` appended to `engine.events`
(capture, chargeback debit, refund, fee, stage change, awaiting, evidence, resolution,
reversal, incident, fine, and `ActionApplied` for each accepted agent action). State
is the fold of the log over the cases' opening state: `DisputeEngine.replay(cases,
events, clock)` rebuilds it exactly without consulting the issuer, and
`state_digest()` / `case_digest(id)` hash it. `events_json()` / `events_from_json()`
serialise the log. Rewind (037): `engine.at(i)` is a new engine after the first `i`
events; `engine.rewind(case_id, k)` restores one case to its state after its first `k`
events (other cases untouched); `rewind_before_action(case_id, k)` goes back to just
before that case's k-th action, e.g. to try a different one.

Extending (032 onwards): add an optional `CaseMix` field whose default draws no extra
randomness (so existing seeds stay byte-identical), any new observable field on
`CardDispute` with a default, and hidden facts on `Truth`. New agent actions add a
model to `AgentAction`, a name to `ACTION_KINDS`, a check in `allowed_actions` and a
branch in `DisputeEngine._apply` that changes state only through `_emit(event)` (a new
event type adds a model to `EngineEvent` and a case in `_apply_event`). The `Issuer`
protocol can be swapped per engine.
"""

from __future__ import annotations

import gc
import hashlib
import os
import random
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from functools import cache
from itertools import accumulate, pairwise
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from scruple.clock import Clock
from scruple.domain import ReasonCode
from scruple.hashing import content_hash
from scruple.sim.card_ledger import CardLedger, DoubleRefundError
from scruple.sim.ce3 import CE3Result, Fingerprint, PriorTransaction, ce3_eligibility
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
    document_problems,
    forge,
    make_document,
)
from scruple.sim.families import (
    CLAIM_VALID,
    FAMILY_CATEGORIES,
    FAMILY_WEIGHTS,
    CaseFamily,
    codes_for,
    family_of,
    statement,
)
from scruple.sim.fees import DEFAULT_FEES, FeeSchedule
from scruple.sim.fx import EXPONENT, FxTable
from scruple.sim.issuer import SeededIssuer, moot
from scruple.sim.lifecycle import (
    OPENING_STAGES,
    Event,
    IllegalTransitionError,
    Lifecycle,
    Transition,
)
from scruple.sim.merchants import MerchantProfile, generate_merchants
from scruple.sim.models import Resolution, Stage
from scruple.sim.monitoring import MonthStatus
from scruple.sim.monitoring import evaluate as evaluate_programs
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
class PendingRefund(BaseModel):
    """A refund the merchant already started, still settling (027)."""

    model_config = _FROZEN

    amount_minor: int = Field(gt=0)
    settles_on: date


class Shipment(BaseModel):
    """One parcel of a split order (028). Tracking status is known to the merchant."""

    model_config = _FROZEN

    shipment_id: str
    amount_minor: int = Field(gt=0)
    delivered_on: date | None  # None: the carrier never confirmed delivery
    disputed: bool  # the cardholder says this parcel did not arrive


class Truth(BaseModel):
    """Hidden ground truth. Never shown to an agent."""

    model_config = _FROZEN

    claim_valid: bool
    liability_minor: int = Field(ge=0)  # what the merchant truly owes, txn currency
    forged: dict[str, Forgery] = Field(default_factory=dict)  # doc_id -> how it was forged
    family: CaseFamily
    statement: str  # what the cardholder says; revealed by RequestInfo
    missing_evidence: tuple[EvidenceKind, ...] = ()  # required kinds lost by the merchant


class CardDispute(BaseModel):
    model_config = _FROZEN

    case_id: str
    txn_id: str
    merchant_id: str
    mcc: str
    network: Network
    reason_code: str
    currency: str  # transaction (presentment) currency
    amount_minor: int = Field(gt=0)  # disputed amount, txn currency
    captured_minor: int = Field(gt=0)
    processed_on: date  # transaction processing date
    opened_on: date
    opening_stage: Stage
    documents: tuple[EvidenceDocument, ...] = ()  # what the merchant holds
    refund_in_flight: PendingRefund | None = None  # 027: a refund racing the dispute
    shipments: tuple[Shipment, ...] = ()  # 028: a split order; empty for one parcel
    settlement_currency: str = "USD"  # 029: the merchant's currency
    fingerprint: Fingerprint | None = None  # 030: CE 3.0 data elements of this txn
    prior_transactions: tuple[PriorTransaction, ...] = ()  # 030: same card, this merchant
    truth: Truth

    @model_validator(mode="after")
    def _consistent(self) -> CardDispute:
        reason_code(self.network, self.reason_code)  # raises KeyError if unknown
        if self.currency not in EXPONENT or self.settlement_currency not in EXPONENT:
            raise ValueError("unsupported currency")
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
        if self.shipments:
            if sum(x.amount_minor for x in self.shipments) != self.captured_minor:
                raise ValueError("shipments must add up to the capture")
            if sum(x.amount_minor for x in self.shipments if x.disputed) != self.amount_minor:
                raise ValueError("the disputed amount must equal the disputed shipments")
        rf = self.refund_in_flight
        if rf is not None and (
            rf.amount_minor > self.captured_minor or rf.settles_on < self.processed_on
        ):
            raise ValueError("refund in flight must fit the capture and follow it")
        if not set(self.truth.forged) <= set(self.evidence_ids):
            raise ValueError("forged ids must name documents of this case")
        fam = self.truth.family
        if self.category not in FAMILY_CATEGORIES[fam]:
            raise ValueError(f"{fam.value} cannot file under a {self.category.value} code")
        if CLAIM_VALID[fam] is not self.truth.claim_valid:
            raise ValueError(f"{fam.value} implies claim_valid={CLAIM_VALID[fam]}")
        return self

    def ce3(self) -> CE3Result:
        """Visa Compelling Evidence 3.0 eligibility (scruple.sim.ce3)."""
        return ce3_eligibility(
            self.network,
            self.reason_code,
            self.processed_on,
            self.fingerprint,
            self.prior_transactions,
        )

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
    """Generator knobs. New features add fields whose defaults leave them off."""

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
    # 026: sample a case family first (see scruple.sim.families.FAMILY_WEIGHTS); when
    # None, pick any card-absent code and draw validity from p_claim_valid
    family_weights: dict[CaseFamily, float] | None = None
    # 027: on a valid claim, the merchant already started a full refund that settles
    # between 5 days before and 10 days after the dispute opens
    p_refund_race: float = Field(0.0, ge=0, le=1)
    # 028: an item-not-received order ships in 2-3 parcels (only some may arrive)
    p_split_shipment: float = Field(0.0, ge=0, le=1)
    # 028: the cardholder disputes only 20-80% of the captured amount
    p_partial_dispute: float = Field(0.0, ge=0, le=1)
    # 029: the cardholder pays in EUR, GBP or JPY; the merchant settles in USD
    p_foreign_currency: float = Field(0.0, ge=0, le=1)
    # 030: a fraud dispute comes with the card's order history at this merchant
    p_ce3_history: float = Field(0.0, ge=0, le=1)
    # 032: evidence availability (A-19). The defaults are the original constants, so
    # existing seeds are unchanged; `scruple.sim.difficulty` moves them together.
    p_required_held_invalid: float = Field(1.0, ge=0, le=1)  # merchant in the right
    p_required_held_valid: float = Field(0.25, ge=0, le=1)  # merchant facing a valid claim
    p_supporting_held_invalid: float = Field(0.6, ge=0, le=1)
    p_supporting_held_valid: float = Field(0.2, ge=0, le=1)


DEFAULT_MIX = CaseMix()
# Every generator feature on at moderate rates (synthetic; A-19 to A-28).
FULL_MIX = CaseMix(
    family_weights=FAMILY_WEIGHTS,
    p_missing_evidence=0.1,
    p_forged_evidence=0.2,
    p_refund_race=0.1,
    p_split_shipment=0.5,
    p_partial_dispute=0.1,
    p_foreign_currency=0.2,
    p_ce3_history=0.6,
)
FX = FxTable()  # synthetic rates (A-27); pass your own to DisputeEngine
START_DATE = date(2026, 1, 5)
CURRENCY = "USD"


_codes_for = cache(codes_for)  # the catalogue is immutable once loaded (038)


@cache
def _cnp_codes(network: Network) -> tuple[ReasonCodeSpec, ...]:
    return tuple(c for c in load_catalogue().for_network(network) if not c.card_present)


def generate_cases(
    seed: int,
    n: int,
    mix: CaseMix = DEFAULT_MIX,
    start: date = START_DATE,
    *,
    id_prefix: str | None = None,
) -> list[CardDispute]:
    """Seeded synthetic disputes. Same (seed, n, mix, start) gives identical cases.

    Case and transaction ids are `cd-<seed>-<i>` unless `id_prefix` is given (033: a
    sealed split must not print its seed into the ids an agent sees).

    The cyclic garbage collector is paused while generating (038): the cases hold no
    reference cycles, and repeated full collections over a growing list of models cost
    about 30% at 20,000 cases. It is re-enabled on return if it was on."""
    with _gc_paused():
        return _generate(seed, n, mix, start, id_prefix)


@contextmanager
def _gc_paused() -> Iterator[None]:
    was_on = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_on:
            gc.enable()


def _generate(
    seed: int, n: int, mix: CaseMix, start: date, id_prefix: str | None
) -> list[CardDispute]:
    rng = random.Random(seed)
    merchants = generate_merchants(seed, mix.n_merchants)
    networks = sorted(mix.network_weights, key=lambda x: x.value)
    # cumulative weights once per call (038); `choices` draws exactly as with `weights`
    net_cum = list(accumulate(mix.network_weights[x] for x in networks))
    fams = sorted(mix.family_weights or {}, key=lambda f: f.value)
    fam_cum = list(accumulate((mix.family_weights or {})[f] for f in fams))
    out: list[CardDispute] = []
    for i in range(n):
        net = rng.choices(networks, cum_weights=net_cum, k=1)[0]
        family: CaseFamily | None = None
        if mix.family_weights is None:
            spec = rng.choice(_cnp_codes(net))
        else:
            family = rng.choices(fams, cum_weights=fam_cum, k=1)[0]
            spec = rng.choice(_codes_for(net, family))
        amount = max(500, int(rng.lognormvariate(4.4, 0.9) * 100))
        processed = start + timedelta(days=rng.randint(0, 60))
        ccy = CURRENCY
        if mix.p_foreign_currency > 0 and rng.random() < mix.p_foreign_currency:
            ccy = rng.choice(("EUR", "GBP", "JPY"))
            amount = max(1, FX.convert(amount, CURRENCY, ccy, processed))
        opened = processed + timedelta(days=rng.randint(3, 90))
        if family is None:
            valid = rng.random() < mix.p_claim_valid
            family = family_of(spec.category, valid)
        else:
            valid = CLAIM_VALID[family]
        stage = Stage.INQUIRY if rng.random() < mix.p_inquiry else Stage.CHARGEBACK
        key = f"{seed if id_prefix is None else id_prefix}-{i:06d}"
        merchant = rng.choice(merchants)
        case_id = f"cd-{key}"
        docs, forged, missing = _documents(rng, case_id, spec, valid, processed, amount, ccy, mix)
        captured, liability = amount, (amount if valid else 0)
        shipments: tuple[Shipment, ...] = ()
        if (
            mix.p_split_shipment > 0
            and spec.legacy_family is ReasonCode.NOT_RECEIVED
            and rng.random() < mix.p_split_shipment
        ):
            shipments, ship_docs = _split(rng, case_id, captured, processed, valid, len(docs), ccy)
            keep = tuple(d for d in docs if d.kind is not EvidenceKind.DELIVERY_PROOF)
            forged = {k: v for k, v in forged.items() if k in {d.doc_id for d in keep}}
            docs = keep + ship_docs
            amount = sum(x.amount_minor for x in shipments if x.disputed)
            liability = sum(
                x.amount_minor for x in shipments if x.disputed and x.delivered_on is None
            )
        elif mix.p_partial_dispute > 0 and rng.random() < mix.p_partial_dispute:
            amount = max(1, captured * rng.randint(20, 80) // 100)
            liability = amount if valid else 0
        fingerprint: Fingerprint | None = None
        priors: tuple[PriorTransaction, ...] = ()
        if (
            mix.p_ce3_history > 0
            and spec.category is Category.FRAUD
            and rng.random() < mix.p_ce3_history
        ):
            fingerprint, priors = _history(rng, case_id, processed, valid)
            fields = {"prior_txn_ids": ",".join(x.txn_id for x in priors), "currency": ccy}
            docs = (
                *docs,
                make_document(
                    case_id,
                    EvidenceKind.PRIOR_UNDISPUTED_HISTORY,
                    processed,
                    fields,
                    len(docs),
                ),
            )
        race: PendingRefund | None = None
        if mix.p_refund_race > 0 and valid and rng.random() < mix.p_refund_race:
            settles = max(processed, opened + timedelta(days=rng.randint(-5, 10)))
            race = PendingRefund(amount_minor=amount, settles_on=settles)
        out.append(
            CardDispute(
                case_id=case_id,
                txn_id=f"tx-{key}",
                merchant_id=merchant.merchant_id,
                mcc=merchant.mcc,
                network=net,
                reason_code=spec.code,
                currency=ccy,
                settlement_currency=CURRENCY,
                amount_minor=amount,
                captured_minor=captured,
                processed_on=processed,
                opened_on=opened,
                opening_stage=stage,
                documents=docs,
                refund_in_flight=race,
                shipments=shipments,
                fingerprint=fingerprint,
                prior_transactions=priors,
                truth=Truth(
                    claim_valid=valid,
                    liability_minor=liability,
                    forged=forged,
                    missing_evidence=missing,
                    family=family,
                    statement=statement(case_id, family),
                ),
            )
        )
    return out


# Chance that the merchant holds each evidence kind (ASSUMPTION A-19). A merchant in
# the right usually holds what the reason code needs; one facing a valid claim rarely
# does. Every case has a receipt and an authorization record. These are the defaults
# of the `CaseMix.p_*_held_*` fields (032).
P_REQUIRED_HELD = {False: 1.0, True: 0.25}  # keyed by claim_valid
P_SUPPORTING_HELD = {False: 0.6, True: 0.2}
ALWAYS_HELD = (EvidenceKind.RECEIPT, EvidenceKind.AUTHORIZATION_RECORD)


def _fields(
    rng: random.Random,
    kind: EvidenceKind,
    processed: date,
    amount: int,
    currency: str,
    valid: bool = False,
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
            f |= {
                "ip": f"203.0.113.{rng.randint(1, 254)}",
                "device_id": f"dev-{ref}",
                # true fraud: the order came from a device the cardholder never used
                "matches_cardholder": "N" if valid else "Y",
            }
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
    p_req = mix.p_required_held_valid if valid else mix.p_required_held_invalid
    p_sup = mix.p_supporting_held_valid if valid else mix.p_supporting_held_invalid
    for k in spec.required_evidence:
        if k not in kinds and rng.random() < p_req:
            kinds.append(k)
    for k in spec.supporting_evidence:
        if k not in kinds and rng.random() < p_sup:
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
        issued, fields = _fields(rng, k, processed, amount, currency, valid)
        d = make_document(case_id, k, issued, fields, idx)
        if k in to_forge:
            how = rng.choice(tuple(Forgery))
            d = forge(d, how, processed)
            forged[d.doc_id] = how
        docs.append(d)
    return tuple(docs), forged, tuple(missing)


def _split(
    rng: random.Random,
    case_id: str,
    captured: int,
    processed: date,
    valid: bool,
    first_index: int,
    currency: str = CURRENCY,
) -> tuple[tuple[Shipment, ...], tuple[EvidenceDocument, ...]]:
    """Split an order into parcels (A-26). A valid claim has at least one lost parcel;
    half the time the cardholder disputes the whole order anyway (an overclaim)."""
    k = rng.randint(2, 3)
    cuts = sorted(rng.sample(range(1, captured), k - 1)) if captured > k else []
    bounds = [0, *cuts, captured]
    amounts = [b - a for a, b in pairwise(bounds)]
    if len(amounts) != k or min(amounts) <= 0:
        amounts = [captured]
    delivered = [True] * len(amounts)
    if valid:
        delivered = [rng.random() < 0.5 for _ in amounts]
        if all(delivered):
            delivered[-1] = False
    overclaim = (not valid) or rng.random() < 0.5
    ships: list[Shipment] = []
    docs: list[EvidenceDocument] = []
    for j, (amt, ok) in enumerate(zip(amounts, delivered, strict=True)):
        sid = f"{case_id}-s{j + 1}"
        day = processed + timedelta(days=rng.randint(2, 8)) if ok else None
        ships.append(
            Shipment(
                shipment_id=sid,
                amount_minor=amt,
                delivered_on=day,
                disputed=overclaim or not ok,
            )
        )
        if day is not None:
            fields = {
                "shipment_id": sid,
                "shipment_amount_minor": str(amt),
                "currency": currency,
                "carrier": rng.choice(("ups", "usps", "dhl")),
                "delivered_on": day.isoformat(),
            }
            docs.append(
                make_document(case_id, EvidenceKind.DELIVERY_PROOF, day, fields, first_index + j)
            )
    return tuple(ships), tuple(docs)


def _history(
    rng: random.Random, case_id: str, processed: date, valid: bool
) -> tuple[Fingerprint, tuple[PriorTransaction, ...]]:
    """The card's earlier orders at this merchant (A-28). Friendly fraud: the disputed
    order shares the cardholder's account, IP and device, and most histories are old
    enough to qualify for CE 3.0. True fraud: the disputed order comes from a new
    device, IP and address, so nothing matches."""
    tag = f"{rng.getrandbits(40):010x}"
    owner = Fingerprint(
        account_id=f"acct-{tag}",
        ip=f"198.51.100.{rng.randint(1, 254)}",
        shipping_address=f"addr-{tag}",
        device_id=f"device-{tag}-{rng.randint(0, 9)}",
    )
    priors: list[PriorTransaction] = []
    for j in range(rng.randint(2, 4)):
        age = rng.randint(121, 364) if rng.random() < 0.8 else rng.randint(10, 119)
        priors.append(
            PriorTransaction(
                txn_id=f"{case_id}-p{j + 1}",
                processed_on=processed - timedelta(days=age),
                disputed=rng.random() < 0.1,
                fingerprint=owner,
            )
        )
    if valid:  # a fraudster used the card
        current = Fingerprint(
            account_id=f"acct-{tag}-x",
            ip=f"192.0.2.{rng.randint(1, 254)}",
            shipping_address=f"addr-{tag}-x",
            device_id=f"device-{tag}-fraud",
        )
    else:
        current = owner
    return current, tuple(priors)


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


class RequestInfo(BaseModel):
    """Ask the issuer for the cardholder's statement. Changes no state."""

    model_config = _FROZEN
    kind: Literal["request_info"] = "request_info"


class Refund(BaseModel):
    """Refund the cardholder directly. Refused if it would refund twice."""

    model_config = _FROZEN
    kind: Literal["refund"] = "refund"
    amount_minor: int = Field(gt=0)


AgentAction = Annotated[
    Accept | Represent | Escalate | RequestInfo | Refund, Field(discriminator="kind")
]
ACTION_KINDS: tuple[str, ...] = ("accept", "represent", "escalate", "request_info", "refund")


class ActionError(StrEnum):
    UNKNOWN_CASE = "unknown_case"
    NOT_OPEN = "not_open"
    RESOLVED = "resolved"
    NOT_ALLOWED = "not_allowed"
    AWAITING_OTHER_PARTY = "awaiting_other_party"
    BAD_EVIDENCE = "bad_evidence"  # unknown, repeated, or another case's evidence id
    DOUBLE_REFUND = "double_refund"  # the ledger refused: it would refund twice


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
    info: dict[str, str] = Field(default_factory=dict)  # e.g. RequestInfo's statement


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
    evidence: tuple[EvidenceDocument, ...] = ()  # every document this case can submit
    refunded_minor: int = 0  # net returned to the cardholder so far (refunds, chargebacks)


class Incident(BaseModel):
    """Something the ledger caught (027)."""

    model_config = _FROZEN

    case_id: str
    on: date
    kind: Literal["double_refund", "refund_blocked"]
    detail: str


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
    settlement_currency: str = "USD"
    merchant_settled_minor: int = 0  # net merchant movement for this txn after capture


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


# -------------------------------------------------------------------------- events
# 036: every state change of a `DisputeEngine` is one of these events. The engine
# state is `fold(genesis(cases), events)`: `DisputeEngine.replay(cases, events, clock)`
# rebuilds it exactly. Decisions (issuer draws, deadline checks) happen when an event
# is produced, never when it is applied, so a replay needs no issuer.
class _Ev(BaseModel):
    model_config = _FROZEN


class CaseCaptured(_Ev):
    type: Literal["captured"] = "captured"
    case_id: str


class ChargebackDebited(_Ev):
    type: Literal["chargeback_debited"] = "chargeback_debited"
    case_id: str
    on: date


class RefundPosted(_Ev):
    type: Literal["refund_posted"] = "refund_posted"
    case_id: str
    amount_minor: int = Field(gt=0)
    on: date


class PendingRefundCleared(_Ev):
    type: Literal["pending_refund_cleared"] = "pending_refund_cleared"
    case_id: str


class FeeCharged(_Ev):
    type: Literal["fee_charged"] = "fee_charged"
    case_id: str
    kind: str
    amount_usd_minor: int = Field(gt=0)
    payee: str


class StageFired(_Ev):
    type: Literal["stage_fired"] = "stage_fired"
    case_id: str
    event: Event
    on: date


class AwaitingSet(_Ev):
    type: Literal["awaiting_set"] = "awaiting_set"
    case_id: str
    awaiting: Awaiting
    due: date | None


class PreArbDeclined(_Ev):
    type: Literal["pre_arb_declined"] = "pre_arb_declined"
    case_id: str
    on: date


class EvidenceSubmitted(_Ev):
    type: Literal["evidence_submitted"] = "evidence_submitted"
    case_id: str
    doc_ids: tuple[str, ...]


class CaseResolved(_Ev):
    type: Literal["case_resolved"] = "case_resolved"
    case_id: str
    resolution: Resolution


class ReversalPosted(_Ev):
    type: Literal["reversal_posted"] = "reversal_posted"
    case_id: str
    amount_minor: int = Field(gt=0)
    on: date


class IncidentRecorded(_Ev):
    type: Literal["incident_recorded"] = "incident_recorded"
    case_id: str
    incident: Incident


class ActionApplied(_Ev):
    """An accepted agent action (the input that caused the events after it)."""

    type: Literal["action_applied"] = "action_applied"
    case_id: str
    action: AgentAction
    on: date


class FinePosted(_Ev):
    type: Literal["fine_posted"] = "fine_posted"
    case_id: None = None
    kind: str
    merchant_id: str
    amount_usd_minor: int = Field(gt=0)
    payee: str
    ref: str


class FinesClosed(_Ev):
    type: Literal["fines_closed"] = "fines_closed"
    case_id: None = None


EngineEvent = Annotated[
    CaseCaptured
    | ChargebackDebited
    | RefundPosted
    | PendingRefundCleared
    | FeeCharged
    | StageFired
    | AwaitingSet
    | PreArbDeclined
    | EvidenceSubmitted
    | CaseResolved
    | ReversalPosted
    | IncidentRecorded
    | ActionApplied
    | FinePosted
    | FinesClosed,
    Field(discriminator="type"),
]
EVENT_LOG = TypeAdapter(tuple[EngineEvent, ...])


class ReplayError(ValueError):
    """An event log that does not fit the cases it is replayed on."""


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
    pending_refund: PendingRefund | None = None
    refunds: int = 0  # merchant refunds posted (for document ids)


class DisputeEngine:
    """Cases, ledger and evidence vault, changed only by appending events (036).

    `events` is the full log; `replay` folds a log back into an identical engine;
    `state_digest` / `case_digest` hash the state so replays can be compared."""

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
        fx: FxTable | None = None,
        events: Sequence[EngineEvent] | None = None,
    ) -> None:
        self.clock = clock
        self.fees = fees if fees is not None else DEFAULT_FEES
        self.issuer: Issuer = issuer if issuer is not None else SeededIssuer()
        self.params = params or EngineParams()
        self.calendar = calendar
        self.merchants = {m.merchant_id: m for m in merchants}
        self._cases_in = list(cases)
        self._fx = fx
        self._genesis()
        if events is None:
            for st in self._cases.values():
                self._open(st)
        else:
            for ev in events:
                self._emit(ev)

    def _genesis(self) -> None:
        """The state before any event: cases at their opening stage, nothing posted."""
        cases = self._cases_in
        self.vault = EvidenceVault(d for c in cases for d in c.documents)
        self.ledger = CardLedger(self._fx or FX)
        self._incidents: list[Incident] = []
        self._fined = False
        self._events: list[EngineEvent] = []
        self._cases: dict[str, _CaseState] = {}
        for c in cases:
            if c.case_id in self._cases:
                raise ValueError(f"duplicate case id {c.case_id}")
            self._cases[c.case_id] = _CaseState(
                case=c,
                life=Lifecycle(c.workflow, c.opened_on, c.opening_stage),
                awaiting="merchant",
                pending_refund=c.refund_in_flight,
            )

    @classmethod
    def replay(
        cls, cases: list[CardDispute], events: Sequence[EngineEvent], clock: Clock, **kwargs: Any
    ) -> DisputeEngine:
        """An engine whose state is the fold of `events` over `cases` (no decisions run).
        Pass the same fees and fx as the original; `issuer` only matters afterwards."""
        return cls(cases, clock, events=events, **kwargs)

    # --- event log ----------------------------------------------------------
    def _emit(self, ev: EngineEvent) -> None:
        """Apply then record. An event that cannot apply is not recorded."""
        try:
            self._apply_event(ev)
        except KeyError as exc:
            raise ReplayError(f"event {ev.type} names an unknown case: {exc}") from exc
        except IllegalTransitionError as exc:
            raise ReplayError(f"event {ev.type} does not fit the state: {exc}") from exc
        self._events.append(ev)

    def _apply_event(self, ev: EngineEvent) -> None:
        """The only place engine state changes."""
        match ev:
            case FinePosted():
                self.ledger.merchant_charge(
                    ev.kind, ev.merchant_id, ev.amount_usd_minor, ev.payee, ev.ref
                )
                return
            case FinesClosed():
                self._fined = True
                return
            case _:
                pass
        st = self._cases[ev.case_id]
        c = st.case
        match ev:
            case CaseCaptured():
                self.ledger.capture(
                    c.txn_id,
                    c.merchant_id,
                    c.captured_minor,
                    c.currency,
                    c.settlement_currency,
                    c.processed_on,
                )
            case ChargebackDebited(on=on):
                self.ledger.chargeback_debit(c.txn_id, c.case_id, c.amount_minor, on)
            case RefundPosted(amount_minor=amount, on=day):
                self.ledger.refund(c.txn_id, c.case_id, amount, day)
                fields = {
                    "amount_minor": str(amount),
                    "currency": c.currency,
                    "settled_on": day.isoformat(),
                    "refund_settled": "Y",
                }
                self.vault.add(
                    make_document(
                        c.case_id, EvidenceKind.REFUND_RECORD, day, fields, 1_000 + st.refunds
                    )
                )
                st.refunds += 1
            case PendingRefundCleared():
                st.pending_refund = None
            case FeeCharged(kind=kind, amount_usd_minor=amount, payee=payee):
                self.ledger.fee(kind, c.txn_id, c.case_id, amount, payee)
            case StageFired(event=event, on=on):
                st.life.fire(event, on)
            case AwaitingSet(awaiting=awaiting, due=due):
                st.awaiting, st.due = awaiting, due
            case PreArbDeclined(on=on):
                st.declined_on = on
            case EvidenceSubmitted(doc_ids=ids):
                st.evidence = self.vault.resolve(c.case_id, ids)
            case CaseResolved(resolution=res):
                st.resolution = res
            case ReversalPosted(amount_minor=amount, on=day):
                self.ledger.to_merchant("chargeback_reversal", c.txn_id, c.case_id, amount, day)
            case IncidentRecorded(incident=inc):
                self._incidents.append(inc)
            case ActionApplied():
                pass  # the input; its effects are the events that follow

    @property
    def events(self) -> tuple[EngineEvent, ...]:
        return tuple(self._events)

    # --- rewind (037) ---------------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        return {
            "fees": self.fees,
            "issuer": self.issuer,
            "merchants": tuple(self.merchants.values()),
            "params": self.params,
            "calendar": self.calendar,
            "fx": self._fx,
        }

    def at(self, index: int, clock: Clock | None = None) -> DisputeEngine:
        """A new engine holding the state after the first `index` events (all cases).
        This engine is unchanged."""
        if not 0 <= index <= len(self._events):
            raise ValueError(f"event index must be in [0, {len(self._events)}]")
        return DisputeEngine.replay(
            self._cases_in, self._events[:index], clock or self.clock, **self._settings()
        )

    def rewind(self, case_id: str, to: int, clock: Clock | None = None) -> tuple[EngineEvent, ...]:
        """Restore one case to its state after its first `to` events. Other cases keep
        their state. Returns the events removed. `to` runs from `opening_events(case_id)`
        (the case as it opened: captured and, for a chargeback, debited) to the number of
        the case's events (no change), and must be one of `step_boundaries(case_id)`.

        The log is refolded without the case's later events, so ledger entry ids after
        the cut are renumbered. Time is the clock's: pass `clock` (e.g. a `ManualClock`
        set to the event's date) to act from that day; otherwise the next `sync` applies
        whatever has fallen due since, deciding again with the issuer."""
        if case_id not in self._cases:
            raise KeyError(case_id)
        mine = [i for i, e in enumerate(self._events) if e.case_id == case_id]
        lo = self.opening_events(case_id)
        if not lo <= to <= len(mine):
            raise ValueError(f"{case_id}: can rewind to {lo}..{len(mine)} events, not {to}")
        if to not in self.step_boundaries(case_id):
            # A cut inside one step (e.g. between a ruling and its reversal) leaves a
            # state the engine never rests in, and continuing from it can crash.
            raise ValueError(f"{case_id}: event {to} is inside a step; rewind to a boundary")
        cut = set(mine[to:])
        kept = [e for i, e in enumerate(self._events) if i not in cut]
        removed = tuple(self._events[i] for i in mine[to:])
        if clock is not None:
            self.clock = clock
        self._genesis()
        for ev in kept:
            self._emit(ev)
        return removed

    def step_boundaries(self, case_id: str) -> tuple[int, ...]:
        """The event counts `rewind` accepts for this case: the end of the opening and
        the end of every later step (an agent action, a refund settling, a missed
        deadline, an issuer reply or a ruling), each of which emits several events."""
        mine = [e for e in self._events if e.case_id == case_id]
        lo = self.opening_events(case_id)
        return tuple(k for k in range(lo, len(mine) + 1) if k == len(mine) or _starts_step(mine[k]))

    def opening_events(self, case_id: str) -> int:
        """How many events open this case (a case cannot be rewound past its opening)."""
        probe = DisputeEngine([self._cases[case_id].case], self.clock, **self._settings())
        return len(probe._events)

    def rewind_before_action(self, case_id: str, k: int, clock: Clock | None = None) -> date:
        """Rewind a case to just before its k-th accepted agent action (0-based) and
        return the day that action was taken."""
        mine = [e for e in self._events if e.case_id == case_id]
        acts = [j for j, e in enumerate(mine) if isinstance(e, ActionApplied)]
        if not 0 <= k < len(acts):
            raise ValueError(f"{case_id} has {len(acts)} actions; no action {k}")
        ev = mine[acts[k]]
        assert isinstance(ev, ActionApplied)
        self.rewind(case_id, acts[k], clock)
        return ev.on

    def case_events(self, case_id: str) -> tuple[tuple[int, EngineEvent], ...]:
        """(global index, event) for every event of one case, in order."""
        return tuple((i, e) for i, e in enumerate(self._events) if e.case_id == case_id)

    def events_json(self) -> str:
        return EVENT_LOG.dump_json(tuple(self._events)).decode("utf-8")

    @staticmethod
    def events_from_json(raw: str | bytes) -> tuple[EngineEvent, ...]:
        return EVENT_LOG.validate_json(raw)

    # --- state hashes ---------------------------------------------------------
    def case_state(self, case_id: str) -> dict[str, object]:
        """Everything the engine holds about one case, as plain data. Ledger entry ids
        are left out (they are global positions), so one case's state does not depend
        on other cases."""
        st = self._cases[case_id]
        c = st.case
        return {
            "stage": st.life.stage.value,
            "entered_on": st.life.entered_on.isoformat(),
            "history": [t.model_dump(mode="json") for t in st.life.history],
            "awaiting": st.awaiting,
            "due": None if st.due is None else st.due.isoformat(),
            "declined_on": None if st.declined_on is None else st.declined_on.isoformat(),
            "evidence": [d.doc_id for d in st.evidence],
            "resolution": None if st.resolution is None else st.resolution.value,
            "pending_refund": None
            if st.pending_refund is None
            else st.pending_refund.model_dump(mode="json"),
            "refunds": st.refunds,
            "returned": self.ledger.returned(c.txn_id),
            "entries": [
                e.model_dump(mode="json", exclude={"entry_id"})
                for e in self.ledger.entries
                if e.txn_id == c.txn_id
            ],
            "violations": [
                v.model_dump(mode="json", exclude={"entry_id"})
                for v in self.ledger.violations
                if v.txn_id == c.txn_id
            ],
            "incidents": [
                i.model_dump(mode="json") for i in self._incidents if i.case_id == case_id
            ],
            "vault": [d.doc_id for d in self.vault.for_case(case_id)],
        }

    def case_digest(self, case_id: str) -> str:
        return content_hash(self.case_state(case_id))

    def state_digest(self) -> str:
        """Hash of the whole engine state: every case, the full ledger (with entry ids),
        violations, incidents and whether fines were posted."""
        return content_hash(
            {
                "cases": {k: self.case_state(k) for k in self._cases},
                "entries": [e.model_dump(mode="json") for e in self.ledger.entries],
                "violations": [v.model_dump(mode="json") for v in self.ledger.violations],
                "incidents": [i.model_dump(mode="json") for i in self._incidents],
                "fined": self._fined,
            }
        )

    # --- queries ------------------------------------------------------------
    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._cases)

    def case(self, case_id: str) -> CardDispute:
        return self._cases[case_id].case

    @property
    def incidents(self) -> tuple[Incident, ...]:
        return tuple(self._incidents)

    def post_monitoring_fines(self) -> tuple[MonthStatus, ...]:
        """Evaluate VAMP and ECM over every known merchant's history and post the fines
        (scruple.sim.monitoring, same entries as `post_fines`). Posts at most once."""
        statuses = tuple(s for m in self.merchants.values() for s in evaluate_programs(m))
        if not self._fined:
            for s in statuses:
                if s.fine_usd_minor > 0:
                    self._emit(
                        FinePosted(
                            kind=f"fine:{s.program.value}",
                            merchant_id=s.merchant_id,
                            amount_usd_minor=s.fine_usd_minor,
                            payee="network:fines",
                            ref=f"{s.program.value}:{s.merchant_id}:{s.month.isoformat()}",
                        )
                    )
            self._emit(FinesClosed())
        return statuses

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
        out.append("request_info")
        out.append("refund")
        return tuple(out)

    def observe(self, case_id: str) -> Observation:
        st = self._cases[case_id]
        self._sync_case(st, today(self.clock))
        return Observation(
            case=st.case.view(),
            today=today(self.clock),
            stage=st.life.stage,
            awaiting="nobody" if st.resolution is not None else st.awaiting,
            deadline=self.deadline(case_id),
            allowed_actions=self.allowed_actions(case_id),
            history=st.life.history,
            merchant=self.merchants.get(st.case.merchant_id),
            evidence=self.vault.for_case(case_id),
            refunded_minor=self.ledger.returned(st.case.txn_id),
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
            settlement_currency=c.settlement_currency,
            merchant_settled_minor=sum(
                p.amount_minor
                for e in self.ledger.entries
                if e.txn_id == c.txn_id and e.kind != "capture" and not e.kind.startswith("fee:")
                for p in e.postings
                if p.account.startswith("merchant:")
            ),
        )

    # --- actions ------------------------------------------------------------
    def act(self, case_id: str, action: AgentAction) -> ActionResult:
        """Apply one agent action. Never raises for a bad action: returns ok=False."""
        if case_id not in self._cases:
            return ActionResult(
                case_id=case_id, action=action.kind, ok=False, error=ActionError.UNKNOWN_CASE
            )
        st = self._cases[case_id]
        now = today(self.clock)
        self._sync_case(st, now)
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
        if isinstance(action, Refund) and action.amount_minor > self.ledger.headroom(
            st.case.txn_id
        ):
            return refuse(
                ActionError.DOUBLE_REFUND,
                f"only {self.ledger.headroom(st.case.txn_id)} is left to refund",
            )
        if isinstance(action, Represent):
            try:
                self.vault.resolve(case_id, action.evidence_ids)
            except EvidenceError as exc:
                return refuse(ActionError.BAD_EVIDENCE, str(exc))
        mark = len(self.ledger.entries)
        self._emit(ActionApplied(case_id=case_id, action=action, on=now))
        info = self._apply(st, action, now)
        posted = tuple(e.entry_id for e in self.ledger.entries[mark:])
        return ActionResult(
            case_id=case_id,
            action=action.kind,
            ok=True,
            stage_before=before,
            stage_after=st.life.stage,
            entries=posted,
            info=info,
        )

    def _apply(self, st: _CaseState, action: AgentAction, now: date) -> dict[str, str]:
        cid = st.case.case_id
        match action:
            case RequestInfo():
                return {"statement": st.case.truth.statement}
            case Refund(amount_minor=amt):
                doc = self._post_refund(st, amt, now)
                return {"refund_doc_id": doc.doc_id} if doc else {}
            case Accept():
                self._resolve(st, Event.ACCEPT, now, Resolution.ACCEPTED)
            case Represent(evidence_ids=ids):
                self._emit(EvidenceSubmitted(case_id=cid, doc_ids=tuple(ids)))
                if st.life.stage is Stage.INQUIRY:
                    if self._won(st, Stage.INQUIRY) == st.case.amount_minor:
                        self._resolve(st, Event.RESOLVE_INQUIRY, now, Resolution.WON)
                    else:
                        self._fire(st, Event.FILE_CHARGEBACK, now)
                        self._on_chargeback(st)
                    return {}
                to = self._fire(st, Event.REPRESENT, now)
                self._enter(st, to, now)
                due = now + timedelta(days=self.params.issuer_response_days)
                self._emit(AwaitingSet(case_id=cid, awaiting="issuer", due=due))
            case Escalate():
                self._fire(st, Event.ESCALATE, now)
                self._enter(st, Stage.ARBITRATION, now)
                due = now + timedelta(days=self.params.arbitration_ruling_days)
                self._emit(AwaitingSet(case_id=cid, awaiting="network", due=due))
        return {}

    def _fire(self, st: _CaseState, event: Event, on: date) -> Stage:
        self._emit(StageFired(case_id=st.case.case_id, event=event, on=on))
        return st.life.stage

    def _open(self, st: _CaseState) -> None:
        c = st.case
        self._emit(CaseCaptured(case_id=c.case_id))
        rf = c.refund_in_flight
        if rf is not None and rf.settles_on <= c.opened_on:
            # the refund settled before the dispute was filed
            self._emit(PendingRefundCleared(case_id=c.case_id))
            self._post_refund(st, rf.amount_minor, rf.settles_on)
        if c.opening_stage is Stage.CHARGEBACK:
            self._on_chargeback(st)

    # --- time ---------------------------------------------------------------
    def sync(self) -> None:
        """Apply every refund settlement, issuer response, ruling and missed deadline due
        by today, in date order per case."""
        now = today(self.clock)
        for st in self._cases.values():
            self._sync_case(st, now)

    def _sync_case(self, st: _CaseState, now: date) -> None:
        """Cases are independent, so one case can be brought up to date on its own."""
        while True:
            rf = st.pending_refund
            if rf is not None and rf.settles_on <= now:
                nxt = self._next_due(st)
                if nxt is None or rf.settles_on <= nxt:
                    self._emit(PendingRefundCleared(case_id=st.case.case_id))
                    self._settle_race(st, rf.amount_minor, rf.settles_on)
                    continue
            if st.resolution is None and self._step_due(st, now):
                continue
            break

    def _next_due(self, st: _CaseState) -> date | None:
        if st.resolution is not None:
            return None
        if st.awaiting == "merchant":
            dl = self.deadline(st.case.case_id)
            return None if dl is None else dl.due + timedelta(days=1)
        return st.due

    def _step_due(self, st: _CaseState, now: date) -> bool:
        cid = st.case.case_id
        if st.awaiting == "merchant":
            dl = self.deadline(cid)
            if dl is None or now <= dl.due:
                return False
            self._missed(st, dl.due + timedelta(days=1))
            return True
        assert st.due is not None
        if now < st.due:
            return False
        day = st.due
        self._emit(AwaitingSet(case_id=cid, awaiting=st.awaiting, due=None))
        if st.awaiting == "network":
            won = self._won(st, Stage.ARBITRATION)
            if won:
                self._resolve(st, Event.RULING, day, self._win_kind(st, won), won)
            else:
                self._resolve(st, Event.RULING, day, Resolution.LOST)
                self._fee(st, "fee:arbitration_loss", self._schedule(st).arbitration_loss)
            return True
        stage = st.life.stage
        won = self._won(st, stage)
        if won:
            self._resolve(st, Event.ISSUER_ACCEPT, day, self._win_kind(st, won), won)
        elif stage is Stage.REPRESENTMENT:
            self._fire(st, Event.ISSUER_PRE_ARB, day)
            self._enter(st, Stage.PRE_ARBITRATION, day)
            self._emit(AwaitingSet(case_id=cid, awaiting="merchant", due=None))
        else:  # allocation pre-arbitration declined by the issuer
            self._emit(PreArbDeclined(case_id=cid, on=day))
            self._emit(AwaitingSet(case_id=cid, awaiting="merchant", due=None))
        return True

    def _missed(self, st: _CaseState, day: date) -> None:
        if st.life.stage is Stage.INQUIRY:
            self._fire(st, Event.DEADLINE_MISSED, day)
            self._on_chargeback(st)
        else:
            self._resolve(st, Event.DEADLINE_MISSED, day, Resolution.LOST)

    # --- money --------------------------------------------------------------
    def _schedule(self, st: _CaseState) -> FeeSchedule:
        return self.fees[st.case.network]

    def _fee(self, st: _CaseState, kind: str, amount: int) -> None:
        if amount < 0:
            raise ValueError("fees cannot be negative")
        if amount == 0:
            return
        payee = "processor:fees" if kind == "fee:chargeback" else "network:fees"
        self._emit(
            FeeCharged(case_id=st.case.case_id, kind=kind, amount_usd_minor=amount, payee=payee)
        )

    def _enter(self, st: _CaseState, stage: Stage, day: date) -> None:
        self._fee(st, f"fee:{stage.value}", self._schedule(st).entry_fee(stage))

    def _on_chargeback(self, st: _CaseState) -> None:
        c = st.case
        before = len(self.ledger.violations)
        self._emit(ChargebackDebited(case_id=c.case_id, on=st.life.entered_on))
        if len(self.ledger.violations) > before:
            v = self.ledger.violations[-1]
            self._emit(
                IncidentRecorded(
                    case_id=c.case_id,
                    incident=Incident(
                        case_id=c.case_id,
                        on=st.life.entered_on,
                        kind="double_refund",
                        detail=f"chargeback debit over-credits the cardholder by {v.over_by}",
                    ),
                )
            )
        self._enter(st, Stage.CHARGEBACK, st.life.entered_on)
        self._emit(AwaitingSet(case_id=c.case_id, awaiting="merchant", due=st.due))

    def _settle_race(self, st: _CaseState, amount: int, day: date) -> None:
        """A refund racing the dispute settles after it opened. If the chargeback debit
        already returned the disputed amount the processor refuses it as a double
        refund (A-23), whether or not the capture has headroom left (a partial dispute
        leaves some): the cardholder is never paid twice for the same dispute."""
        c = st.case
        if self.ledger.returned(c.txn_id) >= c.amount_minor:
            self._emit(
                IncidentRecorded(
                    case_id=c.case_id,
                    incident=Incident(
                        case_id=c.case_id,
                        on=day,
                        kind="refund_blocked",
                        detail=f"refund of {amount} after the chargeback returned the dispute",
                    ),
                )
            )
            return
        self._post_refund(st, amount, day)

    def _post_refund(self, st: _CaseState, amount: int, day: date) -> EvidenceDocument | None:
        """Post a merchant refund; the ledger refuses a double refund (an incident)."""
        c = st.case
        try:
            self._emit(RefundPosted(case_id=c.case_id, amount_minor=amount, on=day))
        except DoubleRefundError as exc:
            self._emit(
                IncidentRecorded(
                    case_id=c.case_id,
                    incident=Incident(
                        case_id=c.case_id, on=day, kind="refund_blocked", detail=str(exc)
                    ),
                )
            )
            return None
        doc = self.vault.for_case(c.case_id)[-1]
        if (
            st.resolution is None
            and st.life.stage is Stage.INQUIRY
            and self.ledger.returned(c.txn_id) >= c.amount_minor
        ):
            self._resolve(
                st, Event.RESOLVE_INQUIRY, max(day, st.life.entered_on), Resolution.ACCEPTED
            )
        return doc

    def _won(self, st: _CaseState, stage: Stage) -> int:
        """How much of the disputed amount the merchant wins at this decision (0: lost).

        For a split order only disputed parcels proven delivered by a genuine, submitted
        delivery document are won (A-26), unless a settled refund of the disputed amount
        makes the dispute moot."""
        c = st.case
        if not self.issuer.merchant_prevails(c, stage, st.evidence):
            return 0
        if not c.shipments or moot(c, st.evidence):
            # a settled refund of the disputed amount answers every parcel (A-23)
            return c.amount_minor
        proven = {
            d.fields.get("shipment_id")
            for d in st.evidence
            if d.kind is EvidenceKind.DELIVERY_PROOF
            and not document_problems(d, c.captured_minor, c.currency, c.processed_on)
        }
        return sum(x.amount_minor for x in c.shipments if x.disputed and x.shipment_id in proven)

    def _win_kind(self, st: _CaseState, won: int) -> Resolution:
        return Resolution.WON if won >= st.case.amount_minor else Resolution.PARTIAL

    def _resolve(
        self, st: _CaseState, event: Event, day: date, res: Resolution, reversal: int = 0
    ) -> None:
        cid = st.case.case_id
        self._fire(st, event, day)
        self._emit(AwaitingSet(case_id=cid, awaiting="nobody", due=st.due))
        self._emit(CaseResolved(case_id=cid, resolution=res))
        if reversal > 0:
            self._emit(ReversalPosted(case_id=cid, amount_minor=reversal, on=day))


def _starts_step(ev: EngineEvent) -> bool:
    """True for the first event of a step after a case has opened (see `rewind`)."""
    match ev:
        case ActionApplied() | PendingRefundCleared():
            return True
        case StageFired(event=Event.DEADLINE_MISSED):
            return True
        case AwaitingSet(awaiting="issuer" | "network", due=None):
            return True
    return False


def drain(engine: DisputeEngine, clock: ManualClock, max_days: int = 800) -> None:
    """Advance the clock one day at a time until every case resolves (merchant idle)."""
    for _ in range(max_days):
        engine.sync()
        if not engine.open_cases():
            return
        clock.advance(days=1)
    raise RuntimeError("cases still open after max_days")  # pragma: no cover


# ------------------------------------------------------------------ sharded generation
SHARD_SIZE = 2_000


def shard_seed(seed: int, shard: int) -> int:
    """The generator seed of one shard: a 64-bit hash of (seed, shard)."""
    digest = hashlib.sha256(f"scruple-shard/{seed}/{shard}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _shard(args: tuple[int, int, int, CaseMix, date]) -> list[CardDispute]:
    seed, shard, n, mix, start = args
    return generate_cases(shard_seed(seed, shard), n, mix, start, id_prefix=f"{seed}s{shard}")


def generate_sharded(
    seed: int,
    n: int,
    mix: CaseMix = DEFAULT_MIX,
    start: date = START_DATE,
    *,
    shard_size: int = SHARD_SIZE,
    workers: int | None = None,
) -> list[CardDispute]:
    """`n` cases in shards of `shard_size`, each from its own derived seed (038).

    Deterministic in (seed, n, mix, start, shard_size) and independent of `workers`
    (processes; None = all cpus, 1 = in this process). Not the same cases as
    `generate_cases(seed, n)`, which draws one stream; case ids are `cd-<seed>s<k>-<i>`."""
    if n < 0 or shard_size < 1:
        raise ValueError("n must be >= 0 and shard_size >= 1")
    jobs = [
        (seed, k, min(shard_size, n - k * shard_size), mix, start)
        for k in range((n + shard_size - 1) // shard_size)
    ]
    w = workers if workers is not None else (os.cpu_count() or 1)
    if w <= 1 or len(jobs) <= 1:
        parts = [_shard(j) for j in jobs]
    else:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=min(w, len(jobs))) as pool, _gc_paused():
            parts = list(pool.map(_shard, jobs))
    return [c for part in parts for c in part]
