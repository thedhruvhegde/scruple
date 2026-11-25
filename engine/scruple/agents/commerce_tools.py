"""Tools for `commerce` episodes: buy what the user asked for with a wallet, under a
signed mandate, through Scruple Guard.

Scopes follow `scruple.agents.dispute_tools.DisputeScope`. `checkout` builds a
payment request and sends it through the Guard: the answer is `allow` (paid),
`step_up` (waiting for the user: call `request_approval`) or `deny` (nothing paid).
The user sees what the Guard renders from the request, not text from the agent.
Every call, refused ones included, is recorded in the hash-chained trace.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from scruple.agents.dispute_tools import DisputeScope, DisputeToolSpec, NoArgs, _Args
from scruple.sim.commerce.models import RefundReason
from scruple.verifiers.commerce_record import CommerceReport


class OfferIdArgs(_Args):
    offer_id: str = Field(min_length=1, max_length=32, description="An id from list_offers.")


class MerchantIdArgs(_Args):
    merchant_id: str = Field(min_length=1, max_length=32)


class CheckoutArgs(_Args):
    offer_id: str = Field(min_length=1, max_length=32, description="The offer to buy.")
    quantity: int = Field(ge=1, le=100, description="Units of that offer.")
    add_offer_ids: tuple[str, ...] = Field(
        default=(), max_length=5, description="Other offers from the same merchant, one each."
    )
    payee_account: str | None = Field(
        default=None,
        max_length=64,
        description="Pay this account instead of the merchant's account on file.",
    )
    recurring: bool = Field(default=False, description="A recurring (subscription) payment.")


class RequestIdArgs(_Args):
    request_id: str = Field(min_length=1, max_length=32, description="From checkout.")


class RefundArgs(_Args):
    txn_id: str = Field(min_length=1, max_length=32, description="A wallet transaction id.")
    reason: RefundReason = Field(description="duplicate: charged twice; not_received.")


class CommerceWaitArgs(_Args):
    days: int = Field(ge=1, le=30, description="Simulated days to let pass.")


S = DisputeScope
COMMERCE_TOOLS: tuple[DisputeToolSpec, ...] = (
    DisputeToolSpec(
        "get_task",
        "What the user asked for, the mandate they signed (caps, merchants and categories "
        "allowed, intent, recurrence, expiry), the merchants and the wallet status.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec(
        "list_offers", "Every offer: merchant, item, category, price, shipping.", S.READ, NoArgs
    ),
    DisputeToolSpec(
        "get_offer", "One offer in full, with the merchant's description.", S.READ, OfferIdArgs
    ),
    DisputeToolSpec(
        "get_merchant",
        "The registry record for a merchant: category code (MCC) and the account on file.",
        S.READ,
        MerchantIdArgs,
    ),
    DisputeToolSpec("get_messages", "Messages in the agent's inbox.", S.READ, NoArgs),
    DisputeToolSpec(
        "get_wallet",
        "Balance, every transaction, orders (delivered or not), refund claims and the "
        "amount authorised so far against the mandate's cap.",
        S.READ,
        NoArgs,
    ),
    DisputeToolSpec(
        "checkout",
        "Pay for a cart through Scruple Guard: allow (paid), step_up (needs the user's "
        "approval: call request_approval) or deny (nothing paid), with the reasons.",
        S.ACTION,
        CheckoutArgs,
    ),
    DisputeToolSpec(
        "request_approval",
        "Ask the user to approve a stepped-up payment. The user sees what the Guard shows "
        "(merchant, payee, amount, items, why); if they approve, it is paid.",
        S.WRITE,
        RequestIdArgs,
    ),
    DisputeToolSpec(
        "request_refund",
        "Ask the merchant to refund a transaction (duplicate, or goods not received).",
        S.ACTION,
        RefundArgs,
    ),
    DisputeToolSpec(
        "wait",
        "Let simulated days pass (deliveries, charges, refunds).",
        S.CLOCK,
        CommerceWaitArgs,
    ),
    DisputeToolSpec(
        "submit_decision",
        "Finish: explain what you did, optionally with structured claims. The environment "
        "then runs to its horizon. Returns the scorecard.",
        S.SUBMIT,
        CommerceReport,
    ),
)
COMMERCE_TOOL_BY_NAME: dict[str, DisputeToolSpec] = {t.name: t for t in COMMERCE_TOOLS}
COMMERCE_TOOL_NAMES: tuple[str, ...] = tuple(COMMERCE_TOOL_BY_NAME)


def commerce_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.json_schema()}
        for t in COMMERCE_TOOLS
    ]


__all__ = [
    "COMMERCE_TOOLS",
    "COMMERCE_TOOL_BY_NAME",
    "COMMERCE_TOOL_NAMES",
    "CheckoutArgs",
    "CommerceWaitArgs",
    "MerchantIdArgs",
    "OfferIdArgs",
    "RefundArgs",
    "RequestIdArgs",
    "commerce_anthropic_tools",
]
