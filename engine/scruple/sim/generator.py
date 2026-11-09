"""Seeded synthetic dispute generator.

All randomness flows from one `random.Random(seed)`. Same seed, same profile, same
n gives byte-identical cases (tests/test_sim.py). Probabilities are synthetic and
chosen to make the edge cases rare, not to match any real portfolio.
"""

from __future__ import annotations

import math
import random
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scruple.domain import ReasonCode
from scruple.sim.models import (
    HIGH_VALUE_THRESHOLD_USD,
    DisputeCase,
    Scenario,
    from_usd,
    to_usd,
)

MCCS = ("5732", "5812", "5999", "4722", "5651")


class Profile(BaseModel):
    """Traffic mix and ground-truth probabilities. Swap profiles to inject drift."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "default"
    weights: dict[Scenario, float] = Field(
        default_factory=lambda: {
            Scenario.FRAUD: 0.32,
            Scenario.NOT_RECEIVED: 0.27,
            Scenario.NOT_AS_DESCRIBED: 0.20,
            Scenario.PARTIAL_REFUND: 0.05,
            Scenario.LATE_EVIDENCE: 0.05,
            Scenario.DUPLICATE_DISPUTE: 0.04,
            Scenario.CURRENCY_MISMATCH: 0.04,
            Scenario.HIGH_VALUE: 0.03,
        }
    )
    p_valid_fraud_3ds: float = 0.05
    p_valid_fraud_no_3ds: float = 0.75
    p_valid_inr_delivered: float = 0.12
    p_valid_inr_undelivered: float = 0.80
    p_valid_nad: float = 0.55
    p_valid_late: float = 0.30
    p_valid_fx: float = 0.60


DEFAULT_PROFILE = Profile()


def generate_cases(
    seed: int, n: int, profile: Profile = DEFAULT_PROFILE, prefix: str = "c"
) -> list[DisputeCase]:
    rng = random.Random(seed)
    scenarios = sorted(profile.weights, key=lambda s: s.value)
    weights = [profile.weights[s] for s in scenarios]
    cases: list[DisputeCase] = []
    i = 0
    while len(cases) < n:
        scenario = rng.choices(scenarios, weights=weights, k=1)[0]
        if scenario is Scenario.DUPLICATE_DISPUTE and len(cases) + 2 > n:
            scenario = Scenario.NOT_RECEIVED
        cases.extend(_make(rng, scenario, profile, f"{prefix}{seed}-{i:06d}"))
        i += 1
    return cases


def _amount_usd(rng: random.Random) -> int:
    # Log-normal around ~$80, clipped below the high-value threshold.
    dollars = rng.lognormvariate(math.log(80), 0.9)
    return max(500, min(int(dollars * 100), HIGH_VALUE_THRESHOLD_USD - 1))


def _days(rng: random.Random, late: bool) -> tuple[int, int]:
    deadline = rng.choice((20, 30))
    ready = deadline + rng.randint(1, 10) if late else rng.randint(1, deadline - 5)
    return ready, deadline


def _make(rng: random.Random, scenario: Scenario, p: Profile, key: str) -> list[DisputeCase]:
    mcc = rng.choice(MCCS)
    merchant = f"m-{rng.randint(1, 40):03d}"
    usd = _amount_usd(rng)
    base: dict[str, Any] = {
        "case_id": f"case-{key}",
        "txn_id": f"txn-{key}",
        "merchant_id": merchant,
        "mcc": mcc,
        "dispute_currency": "USD",
        "txn_currency": "USD",
        "amount_minor": usd,
        "captured_minor": usd,
        "refunded_minor": 0,
        "prior_dispute_on_txn": False,
        "scenario": scenario,
    }
    ready, deadline = _days(rng, late=False)
    base["evidence_ready_day"] = ready
    base["response_deadline_day"] = deadline

    match scenario:
        case Scenario.FRAUD:
            three_ds = rng.random() < 0.5
            pv = p.p_valid_fraud_3ds if three_ds else p.p_valid_fraud_no_3ds
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.FRAUD,
                    three_ds=three_ds,
                    delivery_confirmed=rng.random() < 0.6,
                    claim_valid=rng.random() < pv,
                )
            ]
        case Scenario.NOT_RECEIVED:
            delivered = rng.random() < 0.6
            pv = p.p_valid_inr_delivered if delivered else p.p_valid_inr_undelivered
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.NOT_RECEIVED,
                    three_ds=rng.random() < 0.5,
                    delivery_confirmed=delivered,
                    claim_valid=rng.random() < pv,
                )
            ]
        case Scenario.NOT_AS_DESCRIBED:
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.NOT_AS_DESCRIBED,
                    three_ds=rng.random() < 0.5,
                    delivery_confirmed=rng.random() < 0.9,
                    claim_valid=rng.random() < p.p_valid_nad,
                )
            ]
        case Scenario.PARTIAL_REFUND:
            # Merchant already refunded part; cardholder disputes the full amount.
            base["refunded_minor"] = usd * rng.randint(20, 60) // 100
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.NOT_AS_DESCRIBED,
                    three_ds=rng.random() < 0.5,
                    delivery_confirmed=True,
                    claim_valid=True,
                )
            ]
        case Scenario.LATE_EVIDENCE:
            ready, deadline = _days(rng, late=True)
            base["evidence_ready_day"] = ready
            base["response_deadline_day"] = deadline
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.NOT_RECEIVED,
                    three_ds=rng.random() < 0.5,
                    delivery_confirmed=rng.random() < 0.7,
                    claim_valid=rng.random() < p.p_valid_late,
                )
            ]
        case Scenario.DUPLICATE_DISPUTE:
            # An original not-received dispute followed by a duplicate on the same txn.
            delivered = rng.random() < 0.6
            pv = p.p_valid_inr_delivered if delivered else p.p_valid_inr_undelivered
            original = DisputeCase(
                **{**base, "scenario": Scenario.NOT_RECEIVED},
                reason_code=ReasonCode.NOT_RECEIVED,
                three_ds=rng.random() < 0.5,
                delivery_confirmed=delivered,
                claim_valid=rng.random() < pv,
            )
            dup = original.model_copy(
                update={
                    "case_id": f"case-{key}-dup",
                    "prior_dispute_on_txn": True,
                    "scenario": Scenario.DUPLICATE_DISPUTE,
                    "claim_valid": False,
                }
            )
            return [original, dup]
        case Scenario.CURRENCY_MISMATCH:
            # Settled in EUR/GBP, disputed in USD at a drifted rate (~4% higher).
            ccy = rng.choice(("EUR", "GBP"))
            captured = from_usd(usd, ccy)
            base["txn_currency"] = ccy
            base["captured_minor"] = captured
            base["amount_minor"] = to_usd(captured, ccy) * 104 // 100
            return [
                DisputeCase(
                    **base,
                    reason_code=ReasonCode.FRAUD,
                    three_ds=rng.random() < 0.2,
                    delivery_confirmed=rng.random() < 0.6,
                    claim_valid=rng.random() < p.p_valid_fx,
                )
            ]
        case Scenario.HIGH_VALUE:
            hv = rng.randint(HIGH_VALUE_THRESHOLD_USD + 1, 5 * HIGH_VALUE_THRESHOLD_USD)
            base["amount_minor"] = hv
            base["captured_minor"] = hv
            reason = rng.choice(tuple(ReasonCode))
            return [
                DisputeCase(
                    **base,
                    reason_code=reason,
                    three_ds=rng.random() < 0.5,
                    delivery_confirmed=rng.random() < 0.6,
                    claim_valid=rng.random() < 0.5,
                )
            ]
    raise AssertionError(f"unhandled scenario {scenario}")  # pragma: no cover
