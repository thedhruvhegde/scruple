"""The rule language shared by the registry, simulator and gate.

A learned rule is a structured predicate over the *observable* features of a dispute
case plus an action. Restricting rules to a whitelist of observable fields is a
deliberate security control: a rule cannot reference ground truth, scenario labels or
case identifiers (see docs/DECISIONS.md, D-004 and the leakage attack in
tests/test_adversarial.py).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(StrEnum):
    ACCEPT = "accept"  # accept liability, cardholder is refunded
    CONTEST = "contest"  # represent with evidence
    ESCALATE = "escalate"  # route to a human analyst


class ReasonCode(StrEnum):
    """Synthetic reason codes, loosely modelled on card-network categories."""

    FRAUD = "fraud"
    NOT_RECEIVED = "not_received"
    NOT_AS_DESCRIBED = "not_as_described"


Op = Literal["eq", "ne", "lt", "le", "gt", "ge"]

# Observable fields a rule may reference, and their value types.
RuleField = Literal[
    "mcc",
    "reason_code",
    "amount_usd_minor",
    "delivery_confirmed",
    "three_ds",
    "evidence_on_time",
    "prior_dispute_on_txn",
    "has_prior_refund",
    "currency_mismatch",
]

FIELD_TYPES: dict[str, type] = {
    "mcc": str,
    "reason_code": str,
    "amount_usd_minor": int,
    "delivery_confirmed": bool,
    "three_ds": bool,
    "evidence_on_time": bool,
    "prior_dispute_on_txn": bool,
    "has_prior_refund": bool,
    "currency_mismatch": bool,
}

assert set(FIELD_TYPES) == set(get_args(RuleField))

_ORDER_OPS = {"lt", "le", "gt", "ge"}


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: RuleField
    op: Op
    value: bool | int | str

    @model_validator(mode="after")
    def _type_check(self) -> Condition:
        expected = FIELD_TYPES[self.field]
        # bool is a subclass of int in Python; check it explicitly.
        if expected is int and (isinstance(self.value, bool) or not isinstance(self.value, int)):
            raise ValueError(f"field {self.field!r} needs an integer value")
        if expected is bool and not isinstance(self.value, bool):
            raise ValueError(f"field {self.field!r} needs a boolean value")
        if expected is str and not isinstance(self.value, str):
            raise ValueError(f"field {self.field!r} needs a string value")
        if expected is not int and self.op in _ORDER_OPS:
            raise ValueError(f"operator {self.op!r} is only valid for numeric fields")
        if self.field == "reason_code" and self.value not in {r.value for r in ReasonCode}:
            raise ValueError(f"unknown reason_code {self.value!r}")
        return self

    def matches(self, view: dict[str, bool | int | str]) -> bool:
        actual = view[self.field]
        v = self.value
        match self.op:
            case "eq":
                return actual == v
            case "ne":
                return actual != v
            case "lt":
                return isinstance(actual, int) and isinstance(v, int) and actual < v
            case "le":
                return isinstance(actual, int) and isinstance(v, int) and actual <= v
            case "gt":
                return isinstance(actual, int) and isinstance(v, int) and actual > v
            case "ge":
                return isinstance(actual, int) and isinstance(v, int) and actual >= v

    def describe(self) -> str:
        return f"{self.field} {self.op} {self.value!r}"


class RuleSpec(BaseModel):
    """The executable content of a learned rule. Its hash is the rule's identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=10)
    action: Action
    # Lower number = evaluated first. First matching rule wins.
    priority: int = Field(ge=0, le=1000)

    def matches(self, view: dict[str, bool | int | str]) -> bool:
        return all(c.matches(view) for c in self.conditions)

    def describe(self) -> str:
        conds = " AND ".join(c.describe() for c in self.conditions)
        return f"IF {conds} THEN {self.action.value} (priority {self.priority})"
