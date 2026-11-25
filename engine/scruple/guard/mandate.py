"""Mandates and payment requests for agent-initiated payments (Scruple Guard).

A `Mandate` is what the user signed: which agent may spend, how much per payment and
in total, at which merchants or merchant categories, on what (the intent: item
categories, quantities and unit prices), whether payments may recur, in which
currency and until when. A `PaymentRequest` is one payment an agent wants to make
under it. `scruple.guard.rules.authorize` checks one against the other.

ASSUMPTIONS (we could not read the protocol specifications from this environment;
every field below is our model, not a wire format):

- A-G1 The mandate stands for the user-signed intent in agent payment protocols:
  Google's Agent Payments Protocol (AP2) describes signed "intent" and "cart"
  mandates; the Stripe / OpenAI Agentic Commerce Protocol describes a delegated
  payment token limited by amount and expiry; Visa Intelligent Commerce and
  Mastercard Agent Pay describe agent-bound network tokens with spending controls;
  Coinbase x402 settles per HTTP request (`402 Payment Required`). We model the
  common core (caps, merchant and category limits, intent items, recurrence,
  expiry) and none of their encodings.
- A-G2 The signature is an HMAC-SHA256 over the canonical JSON of the mandate body
  with a key the user (or the user's wallet) holds. Real deployments use asymmetric
  signatures or verifiable credentials so the verifier does not hold the signing
  key; the check is the same shape (signature over a canonical body, verified before
  anything else).
- A-G3 Amounts are integer minor units in one currency per mandate; no FX.
- A-G4 The cumulative cap counts gross authorised spend: a refund does not restore
  headroom. This is conservative by design (it makes buy-refund-buy cycles unable to
  exceed the cap, see docs/math/10_cumulative_spend.md) and is a policy choice a
  deployment could relax.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scruple.hashing import canonical_json, content_hash

_FROZEN = ConfigDict(frozen=True, extra="forbid")
MANDATE_VERSION: Literal["scruple-mandate/0"] = "scruple-mandate/0"


class IntentItem(BaseModel):
    """One thing the user asked for: a category, how many and the most per unit."""

    model_config = _FROZEN

    category: str = Field(min_length=1, max_length=64)
    max_quantity: int = Field(ge=1, le=1_000)
    max_unit_minor: int = Field(gt=0)


class RecurrenceTerms(BaseModel):
    """Recurring payments the user allowed: one merchant, at most this much each time,
    at least this many days apart, at most this many times."""

    model_config = _FROZEN

    merchant_id: str = Field(min_length=1, max_length=64)
    max_amount_minor: int = Field(gt=0)
    min_interval_days: int = Field(ge=1, le=366)
    max_occurrences: int = Field(ge=1, le=1_000)


class Mandate(BaseModel):
    model_config = _FROZEN

    version: Literal["scruple-mandate/0"] = MANDATE_VERSION
    mandate_id: str = Field(min_length=1, max_length=64)
    principal_id: str = Field(min_length=1, max_length=64)  # the user
    agent_id: str = Field(min_length=1, max_length=64)  # the only agent that may spend
    currency: str = Field(min_length=3, max_length=3)
    issued_at: datetime
    expires_at: datetime
    per_txn_cap_minor: int = Field(gt=0)
    cumulative_cap_minor: int = Field(gt=0)
    # above this, a payment needs the user's own approval (human in the loop)
    step_up_above_minor: int | None = Field(default=None, gt=0)
    allowed_merchants: tuple[str, ...] = ()  # empty: any merchant in the registry
    allowed_mccs: tuple[str, ...] = ()  # empty: any category code
    blocked_mccs: tuple[str, ...] = ()
    items: tuple[IntentItem, ...] = ()  # empty: no cart constraint
    recurrence: RecurrenceTerms | None = None
    duplicate_window_hours: int = Field(default=24, ge=0, le=24 * 30)
    signature: str = ""  # hex HMAC-SHA256 over `body()` (A-G2)

    @model_validator(mode="after")
    def _consistent(self) -> Mandate:
        if self.expires_at <= self.issued_at:
            raise ValueError("a mandate must expire after it is issued")
        if self.per_txn_cap_minor > self.cumulative_cap_minor:
            raise ValueError("the per-payment cap exceeds the cumulative cap")
        if len({i.category for i in self.items}) != len(self.items):
            raise ValueError("intent items must name distinct categories")
        return self

    def body(self) -> dict[str, object]:
        """What the signature covers: everything but the signature."""
        return self.model_dump(mode="json", exclude={"signature"})

    def digest(self) -> str:
        return content_hash(self.body())

    def item(self, category: str) -> IntentItem | None:
        return next((i for i in self.items if i.category == category), None)


def _mac(key: str | bytes, body: object) -> str:
    k = key.encode() if isinstance(key, str) else key
    return hmac.new(k, canonical_json(body).encode(), sha256).hexdigest()


def sign_mandate(mandate: Mandate, key: str | bytes) -> Mandate:
    return mandate.model_copy(update={"signature": _mac(key, mandate.body())})


def signature_ok(mandate: Mandate, key: str | bytes) -> bool:
    return bool(mandate.signature) and hmac.compare_digest(
        mandate.signature, _mac(key, mandate.body())
    )


class CartLine(BaseModel):
    model_config = _FROZEN

    sku: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=1_000)
    unit_minor: int = Field(ge=0)

    @property
    def total_minor(self) -> int:
        return self.quantity * self.unit_minor


class PaymentRequest(BaseModel):
    """One payment an agent asks to make. `amount_minor` is what would be charged; it
    must equal the cart (lines + shipping + tax)."""

    model_config = _FROZEN

    request_id: str = Field(min_length=1, max_length=64)
    mandate_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(min_length=1, max_length=64)
    merchant_id: str = Field(min_length=1, max_length=64)
    mcc: str = Field(min_length=1, max_length=8)
    payee_account: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int = Field(gt=0)
    lines: tuple[CartLine, ...] = ()
    shipping_minor: int = Field(default=0, ge=0)
    tax_minor: int = Field(default=0, ge=0)
    recurring: bool = False
    idempotency_key: str = Field(min_length=1, max_length=128)
    at: datetime

    def cart_minor(self) -> int:
        return sum(line.total_minor for line in self.lines) + self.shipping_minor + self.tax_minor

    def cart_digest(self) -> str:
        """Same merchant, same lines, same amount: what duplicate detection compares."""
        return content_hash(
            {
                "merchant": self.merchant_id,
                "amount": self.amount_minor,
                "lines": sorted((line.sku, line.quantity, line.unit_minor) for line in self.lines),
            }
        )

    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class MerchantRecord(BaseModel):
    """What the registry (the wallet provider's merchant directory) holds about a
    merchant: its category code and the account payments to it settle to."""

    model_config = _FROZEN

    merchant_id: str
    mcc: str
    payee_account: str


__all__ = [
    "MANDATE_VERSION",
    "CartLine",
    "IntentItem",
    "Mandate",
    "MerchantRecord",
    "PaymentRequest",
    "RecurrenceTerms",
    "sign_mandate",
    "signature_ok",
]
