"""Map Stripe disputes into scruple's internal records.

Status and reason values come from the vendored Stripe spec (`dispute.status` enum and
the values listed in `dispute.reason`'s description); the contract test fails when a
re-vendored spec adds a value missing here. Unknown values at runtime raise
`UnmappedValueError` rather than being guessed or dropped.

ASSUMPTION: the stage and category mappings below are scruple's own modelling choices
(our lifecycle is synthetic, see docs/ASSUMPTIONS.md), not Stripe or network rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from scruple.domain import ReasonCode
from scruple.ingest.currency import normalise_currency
from scruple.ingest.errors import (
    IngestError,
    LiveModeRefusedError,
    SchemaMismatchError,
    UnmappedValueError,
)
from scruple.ingest.limits import load_json_object
from scruple.ingest.models import CaseState, ExternalCase, IngestBatch, LedgerMovement, MovementKind
from scruple.ingest.stripe.wire import BalanceTransaction, Dispute
from scruple.sim.models import Stage

SOURCE = "stripe"

STATUS_MAP: Mapping[str, tuple[Stage, CaseState]] = {
    "warning_needs_response": (Stage.INQUIRY, CaseState.OPEN),
    "warning_under_review": (Stage.INQUIRY, CaseState.OPEN),
    "warning_closed": (Stage.RESOLVED, CaseState.CLOSED),
    "needs_response": (Stage.CHARGEBACK, CaseState.OPEN),
    "under_review": (Stage.REPRESENTMENT, CaseState.OPEN),
    "won": (Stage.RESOLVED, CaseState.WON),
    "lost": (Stage.RESOLVED, CaseState.LOST),
    "prevented": (Stage.RESOLVED, CaseState.PREVENTED),
}

# Stripe reason -> scruple's three synthetic categories, or None (kept verbatim only).
REASON_MAP: Mapping[str, ReasonCode | None] = {
    "bank_cannot_process": None,
    "check_returned": None,
    "credit_not_processed": None,
    "customer_initiated": None,
    "debit_not_authorized": None,
    "duplicate": None,
    "fraudulent": ReasonCode.FRAUD,
    "general": None,
    "incorrect_account_details": None,
    "insufficient_funds": None,
    "noncompliant": None,
    "product_not_received": ReasonCode.NOT_RECEIVED,
    "product_unacceptable": ReasonCode.NOT_AS_DESCRIBED,
    "subscription_canceled": None,
    "unrecognized": None,
}

# Every documented `dispute_evidence` field whose presence scruple records.
EVIDENCE_FIELDS: tuple[str, ...] = (
    "access_activity_log",
    "billing_address",
    "cancellation_policy",
    "cancellation_policy_disclosure",
    "cancellation_rebuttal",
    "customer_communication",
    "customer_email_address",
    "customer_name",
    "customer_purchase_ip",
    "customer_signature",
    "duplicate_charge_documentation",
    "duplicate_charge_explanation",
    "duplicate_charge_id",
    "product_description",
    "receipt",
    "refund_policy",
    "refund_policy_disclosure",
    "refund_refusal_explanation",
    "service_date",
    "service_documentation",
    "shipping_address",
    "shipping_carrier",
    "shipping_date",
    "shipping_documentation",
    "shipping_tracking_number",
    "uncategorized_file",
    "uncategorized_text",
)


def _ts(seconds: int, what: str) -> datetime:
    # Year 1970..9999 keeps datetime happy and rejects absurd values.
    if not 0 <= seconds <= 253_402_300_799:
        raise SchemaMismatchError(f"{what} timestamp {seconds} is out of range")
    return datetime.fromtimestamp(seconds, UTC)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != {} and value != []


def parse_dispute(obj: Mapping[str, Any] | bytes | str) -> Dispute:
    """Validate a raw Stripe dispute (dict or JSON bytes) into the wire model."""
    raw = obj if isinstance(obj, Mapping) else load_json_object(obj, what="Stripe dispute")
    try:
        return Dispute.model_validate(raw)
    except ValidationError as e:
        raise SchemaMismatchError(f"not a Stripe dispute: {e.error_count()} errors") from None


def map_dispute(d: Dispute, *, allow_live: bool = False) -> ExternalCase:
    if d.livemode and not allow_live:
        raise LiveModeRefusedError(f"dispute {d.id} is live mode; only test mode is enabled")
    try:
        stage, state = STATUS_MAP[d.status]
    except KeyError:
        raise UnmappedValueError(f"unknown Stripe dispute status {d.status!r}") from None
    if d.reason not in REASON_MAP:
        raise UnmappedValueError(f"unknown Stripe dispute reason {d.reason!r}")
    if d.amount < 0:
        raise SchemaMismatchError(f"dispute {d.id} has a negative amount")
    evidence = tuple(f for f in EVIDENCE_FIELDS if _present(d.evidence.get(f)))
    card = d.payment_method_details.card if d.payment_method_details else None
    due = d.evidence_details.due_by
    try:
        return ExternalCase(
            source=SOURCE,
            external_id=d.id,
            payment_ref=d.charge_id,
            stage=stage,
            state=state,
            reason_code=REASON_MAP[d.reason],
            source_reason=d.reason,
            network=card.network if card else None,
            network_reason_code=card.network_reason_code if card else None,
            amount_minor=d.amount,
            currency=normalise_currency(d.currency),
            opened_at=_ts(d.created, "created"),
            # Stripe documents due_by == 0 as "no response allowed".
            evidence_due_at=_ts(due, "due_by") if due else None,
            evidence_fields=evidence,
            evidence_submitted=d.evidence_details.submission_count > 0,
            evidence_past_due=d.evidence_details.past_due,
            livemode=d.livemode,
        )
    except ValidationError as e:
        raise SchemaMismatchError(
            f"dispute {d.id} does not map: {e.error_count()} errors"
        ) from None


def map_balance_transaction(bt: BalanceTransaction, d: Dispute) -> LedgerMovement:
    if bt.net != bt.amount - bt.fee:
        # Stripe documents net as amount - fee.
        raise SchemaMismatchError(f"balance transaction {bt.id}: net != amount - fee")
    if bt.fee < 0:
        raise SchemaMismatchError(f"balance transaction {bt.id}: negative fee")
    kind = (
        MovementKind.DISPUTE_DEBIT
        if bt.amount < 0
        else MovementKind.DISPUTE_CREDIT
        if bt.amount > 0
        else MovementKind.OTHER
    )
    try:
        return LedgerMovement(
            source=SOURCE,
            external_id=bt.id,
            case_external_id=d.id,
            payment_ref=d.charge_id,
            kind=kind,
            amount_minor=bt.amount,
            fee_minor=bt.fee,
            net_minor=bt.net,
            currency=normalise_currency(bt.currency),
            occurred_at=_ts(bt.created, "created"),
            category=bt.reporting_category,
        )
    except ValidationError as e:
        raise SchemaMismatchError(
            f"balance transaction {bt.id}: {e.error_count()} errors"
        ) from None


def ingest_dispute(
    obj: Mapping[str, Any] | bytes | str, *, allow_live: bool = False
) -> IngestBatch:
    """Raw Stripe dispute -> one case plus its balance movements. Raises `IngestError`."""
    d = parse_dispute(obj)
    case = map_dispute(d, allow_live=allow_live)
    moves = tuple(map_balance_transaction(bt, d) for bt in d.balance_transactions)
    return IngestBatch(cases=(case,), movements=moves)


__all__ = [
    "EVIDENCE_FIELDS",
    "REASON_MAP",
    "STATUS_MAP",
    "IngestError",
    "ingest_dispute",
    "map_balance_transaction",
    "map_dispute",
    "parse_dispute",
]
