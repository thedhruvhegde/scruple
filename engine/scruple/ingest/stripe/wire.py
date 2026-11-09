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


class ThreeDSecureDetailsCharge(_Wire):
    result: str | None = Field(default=None, max_length=64)
    result_reason: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=16)
    authentication_flow: str | None = Field(default=None, max_length=32)


class CardChecks(_Wire):
    address_line1_check: str | None = Field(default=None, max_length=32)
    address_postal_code_check: str | None = Field(default=None, max_length=32)
    cvc_check: str | None = Field(default=None, max_length=32)


class ChargeCard(_Wire):
    # Not read: last4, fingerprint, exp_*, network_transaction_id (identifiers).
    brand: str | None = Field(default=None, max_length=64)
    funding: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=8)
    checks: CardChecks | None = None
    three_d_secure: ThreeDSecureDetailsCharge | None = None


class ChargePaymentMethodDetails(_Wire):
    type: str = Field(max_length=64)
    card: ChargeCard | None = None


class Address(_Wire):
    # Only whether the address is there is ever kept; see mapping.payment_facts.
    line1: str | None = Field(default=None, max_length=5000)
    postal_code: str | None = Field(default=None, max_length=5000)
    country: str | None = Field(default=None, max_length=5000)


class Shipping(_Wire):
    address: Address | None = None
    carrier: str | None = Field(default=None, max_length=5000)
    tracking_number: str | None = Field(default=None, max_length=5000)


class Refund(_Wire):
    id: str = Field(min_length=1, max_length=255)
    amount: StrictInt
    created: StrictInt
    status: str | None = Field(default=None, max_length=32)


class RefundList(_Wire):
    data: tuple[Refund, ...] = Field(max_length=100)
    has_more: StrictBool


class Charge(_Wire):
    id: str = Field(min_length=1, max_length=255)
    amount: StrictInt
    amount_captured: StrictInt
    amount_refunded: StrictInt
    currency: str = Field(max_length=8)
    created: StrictInt
    livemode: StrictBool
    refunded: StrictBool | None = None
    payment_method_details: ChargePaymentMethodDetails | None = None
    shipping: Shipping | None = None
    refunds: RefundList | None = None


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
    enhanced_eligibility_types: tuple[str, ...] = Field(default=(), max_length=16)
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
    ChargePaymentMethodDetails: "payment_method_details",
    ChargeCard: "payment_method_details_card",
    CardChecks: "payment_method_details_card_checks",
    ThreeDSecureDetailsCharge: "three_d_secure_details_charge",
    Shipping: "shipping",
    Address: "address",
    Refund: "refund",
    RefundList: "charge#refunds",  # an inline object in the charge schema
    DisputeEvidenceDetails: "dispute_evidence_details",
    DisputePaymentMethodDetailsCard: "dispute_payment_method_details_card",
    DisputePaymentMethodDetails: "dispute_payment_method_details",
    Dispute: "dispute",
    Event: "event",
    EventData: "notification_event_data",
}
