"""Case families: who is really at fault behind a dispute (hidden ground truth).

- true fraud: the card was used without the cardholder's consent; the claim is valid.
- friendly fraud: the cardholder (or someone in their household) made the purchase
  and disputes it as fraud anyway; the claim is invalid. The merchant usually holds
  device, IP and account evidence linking the cardholder to the order.
- service failure / buyer's remorse: consumer disputes that are valid / invalid.
- merchant error / cardholder mistake: processing and authorization disputes that are
  valid / invalid.

Family weights, statements and their odds are ASSUMPTIONS (docs/ASSUMPTIONS.md A-22).
"""

from __future__ import annotations

import random
from enum import StrEnum

from scruple.sim.networks import Category, Network
from scruple.sim.reason_codes import ReasonCodeSpec, load_catalogue


class CaseFamily(StrEnum):
    TRUE_FRAUD = "true_fraud"
    FRIENDLY_FRAUD = "friendly_fraud"
    SERVICE_FAILURE = "service_failure"
    BUYERS_REMORSE = "buyers_remorse"
    MERCHANT_ERROR = "merchant_error"
    CARDHOLDER_MISTAKE = "cardholder_mistake"


CLAIM_VALID: dict[CaseFamily, bool] = {
    CaseFamily.TRUE_FRAUD: True,
    CaseFamily.FRIENDLY_FRAUD: False,
    CaseFamily.SERVICE_FAILURE: True,
    CaseFamily.BUYERS_REMORSE: False,
    CaseFamily.MERCHANT_ERROR: True,
    CaseFamily.CARDHOLDER_MISTAKE: False,
}

FAMILY_CATEGORIES: dict[CaseFamily, frozenset[Category]] = {
    CaseFamily.TRUE_FRAUD: frozenset({Category.FRAUD}),
    CaseFamily.FRIENDLY_FRAUD: frozenset({Category.FRAUD}),
    CaseFamily.SERVICE_FAILURE: frozenset({Category.CONSUMER_DISPUTE}),
    CaseFamily.BUYERS_REMORSE: frozenset({Category.CONSUMER_DISPUTE}),
    CaseFamily.MERCHANT_ERROR: frozenset({Category.PROCESSING_ERROR, Category.AUTHORIZATION}),
    CaseFamily.CARDHOLDER_MISTAKE: frozenset({Category.PROCESSING_ERROR, Category.AUTHORIZATION}),
}

# Suggested mix for `CaseMix(family_weights=...)`. Synthetic (A-22).
FAMILY_WEIGHTS: dict[CaseFamily, float] = {
    CaseFamily.TRUE_FRAUD: 0.22,
    CaseFamily.FRIENDLY_FRAUD: 0.28,
    CaseFamily.SERVICE_FAILURE: 0.18,
    CaseFamily.BUYERS_REMORSE: 0.20,
    CaseFamily.MERCHANT_ERROR: 0.06,
    CaseFamily.CARDHOLDER_MISTAKE: 0.06,
}

# What the cardholder says when asked (RequestInfo). Overlapping on purpose: a
# statement is a weak signal, never proof (A-22).
STATEMENTS: dict[CaseFamily, tuple[tuple[str, float], ...]] = {
    CaseFamily.TRUE_FRAUD: (
        ("my card was lost or stolen", 0.4),
        ("I did not make this purchase", 0.4),
        ("I do not recognise this charge", 0.2),
    ),
    CaseFamily.FRIENDLY_FRAUD: (
        ("I do not recognise this charge", 0.5),
        ("I did not make this purchase", 0.3),
        ("someone in my household used my card", 0.2),
    ),
    CaseFamily.SERVICE_FAILURE: (
        ("the order never arrived", 0.4),
        ("the item was not as described", 0.4),
        ("I cancelled and was still charged", 0.2),
    ),
    CaseFamily.BUYERS_REMORSE: (
        ("the order never arrived", 0.3),
        ("the item was not as described", 0.4),
        ("I cancelled and was still charged", 0.3),
    ),
    CaseFamily.MERCHANT_ERROR: (("I was charged twice", 0.5), ("the amount is wrong", 0.5)),
    CaseFamily.CARDHOLDER_MISTAKE: (("I was charged twice", 0.5), ("the amount is wrong", 0.5)),
}


def family_of(category: Category, claim_valid: bool) -> CaseFamily:
    """The family implied by a category and validity (used when no mix is given)."""
    for fam, cats in FAMILY_CATEGORIES.items():
        if category in cats and CLAIM_VALID[fam] is claim_valid:
            return fam
    raise AssertionError(category)  # pragma: no cover - the table covers every category


def codes_for(network: Network, family: CaseFamily) -> tuple[ReasonCodeSpec, ...]:
    """Card-absent reason codes a family files under on a network."""
    return tuple(
        c
        for c in load_catalogue().for_network(network)
        if not c.card_present and c.category in FAMILY_CATEGORIES[family]
    )


def statement(case_id: str, family: CaseFamily) -> str:
    """Deterministic per case, from its own stream (does not disturb the generator)."""
    options = STATEMENTS[family]
    rng = random.Random(f"statement:{case_id}")
    return rng.choices([s for s, _ in options], weights=[w for _, w in options], k=1)[0]
