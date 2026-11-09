"""Stripe API objects as scruple reads them (a strict subset of the real schema).

Field names and types follow the vendored Stripe OpenAPI spec in
tests/fixtures/stripe/spec3.subset.json (stripe/openapi, API version 2026-09-30.endive);
tests/test_ingest_stripe.py checks every field declared here against that spec.

Unknown fields are ignored on purpose: Stripe adds fields without a version bump,
and we only depend on the ones declared here. Missing or mistyped declared fields
are errors. Integers and booleans are strict, so "1000" is never read as 1000.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Fee(_Wire):
    amount: StrictInt
    currency: str = Field(max_length=8)
    type: str = Field(max_length=64)


class BalanceTransaction(_Wire):
    id: str = Field(min_length=1, max_length=255)
    object: Literal["balance_transaction"]
    amount: StrictInt
    currency: str = Field(max_length=8)
    created: StrictInt
    fee: StrictInt
    fee_details: tuple[Fee, ...] = Field(max_length=64)
    net: StrictInt
    reporting_category: str = Field(max_length=64)
    status: str = Field(max_length=32)
    type: str = Field(max_length=64)


class Charge(_Wire):
    id: str = Field(min_length=1, max_length=255)
    amount: StrictInt
    amount_captured: StrictInt
    amount_refunded: StrictInt
    currency: str = Field(max_length=8)
    created: StrictInt
    livemode: StrictBool


class DisputeEvidenceDetails(_Wire):
    due_by: StrictInt | None = None
    has_evidence: StrictBool
    past_due: StrictBool
    submission_count: StrictInt


class DisputePaymentMethodDetailsCard(_Wire):
    brand: str = Field(max_length=64)
    case_type: str = Field(max_length=32)
    network: str = Field(max_length=64)
    network_reason_code: str | None = Field(default=None, max_length=64)


class DisputePaymentMethodDetails(_Wire):
    type: str = Field(max_length=32)
    card: DisputePaymentMethodDetailsCard | None = None


class Dispute(_Wire):
    id: str = Field(min_length=1, max_length=255)
    object: Literal["dispute"]
    amount: StrictInt
    currency: str = Field(max_length=8)
    created: StrictInt
    charge: str | Charge
    reason: str = Field(max_length=64)
    status: str = Field(max_length=64)
    livemode: StrictBool
    is_charge_refundable: StrictBool
    balance_transactions: tuple[BalanceTransaction, ...] = Field(max_length=16)
    # Only the presence of each evidence field is read, never its content.
    evidence: dict[str, Any]
    evidence_details: DisputeEvidenceDetails
    payment_method_details: DisputePaymentMethodDetails | None = None
    metadata: dict[str, str]

    @property
    def charge_id(self) -> str:
        return self.charge if isinstance(self.charge, str) else self.charge.id


class DisputeList(_Wire):
    object: Literal["list"]
    data: tuple[Dispute, ...] = Field(max_length=100)
    has_more: StrictBool
    url: str = Field(max_length=255)


class EventData(_Wire):
    object: dict[str, Any]


class Event(_Wire):
    id: str = Field(min_length=1, max_length=255)
    object: Literal["event"]
    type: str = Field(max_length=128)
    created: StrictInt
    livemode: StrictBool
    api_version: str | None = Field(default=None, max_length=64)
    data: EventData


# Which vendored schema each wire model corresponds to (used by the contract test).
SCHEMA_OF: dict[type[BaseModel], str] = {
    Fee: "fee",
    BalanceTransaction: "balance_transaction",
    Charge: "charge",
    DisputeEvidenceDetails: "dispute_evidence_details",
    DisputePaymentMethodDetailsCard: "dispute_payment_method_details_card",
    DisputePaymentMethodDetails: "dispute_payment_method_details",
    Dispute: "dispute",
    Event: "event",
    EventData: "notification_event_data",
}
