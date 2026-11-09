"""The decision-time view of a historical case: only what was known when it was decided.

No look-ahead, by construction. Every fact about a case carries the time it became
known; `build_view` keeps the facts known at or before the decision time and drops the
rest. Dropped facts are not stored in the view at all, so a policy cannot reach them
through private attributes either; asking for one by name raises `LookAheadError`.

Known when a dispute opens: its reason (verbatim and as a scruple category), network
and network reason code, amount and currency, the open time, the evidence due date,
and how many earlier disputes the same payment had (counted over cases opened strictly
before the decision time). Ledger postings are visible only up to the decision time
(normally the dispute debit and its fee).

Never known at decision time, because exports do not timestamp them: the final state
and stage, whether and when evidence was submitted, which evidence fields, whether it
was late, and anything derived from the outcome. A later posting (a reinstatement
credit) is likewise out of the view.

`rule_view()` gives the observable-field dictionary that `scruple.domain.RuleSpec`
conditions read. Exports do not carry some of those fields; they are filled with fixed
defaults and listed in `DEFAULTED_RULE_FIELDS` (and on every report).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scruple.backtest.history import FEE_ACCOUNT, HistoricalCase
from scruple.sim.models import FX_TO_USD_PER_100, to_usd

# Facts no export timestamps: never visible before the outcome.
OUTCOME_FACTS = (
    "state",
    "stage",
    "evidence_submitted",
    "evidence_fields",
    "evidence_past_due",
    "outcome",
    "historical_action",
    "ledger_after_decision",
)
# RuleSpec fields an export does not carry, and the value used instead.
DEFAULTED_RULE_FIELDS: Mapping[str, bool | str] = {
    "mcc": "unknown",
    "delivery_confirmed": False,
    "three_ds": False,
    "has_prior_refund": False,
    "currency_mismatch": False,
}


class LookAheadError(RuntimeError):
    """A policy asked for a fact that was not known at decision time."""


@dataclass(frozen=True)
class Fact:
    name: str
    value: Any
    known_at: datetime | None  # None: never known before the outcome


@dataclass(frozen=True)
class Posting:
    """A ledger posting as the policy may see it (merchant side only)."""

    occurred_at: datetime
    merchant_net_minor: int  # negative: money left the merchant
    fee_minor: int


class DecisionView:
    """Read-only facts known at `decided_at`. Attribute and item access both work."""

    __slots__ = ("_values", "_withheld", "decided_at")

    _values: dict[str, Any]
    _withheld: dict[str, datetime | None]
    decided_at: datetime

    def __init__(
        self,
        values: Mapping[str, Any],
        withheld: Mapping[str, datetime | None],
        decided_at: datetime,
    ) -> None:
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "_withheld", dict(withheld))
        object.__setattr__(self, "decided_at", decided_at)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a decision view is read-only")

    def _lookup(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        if name in self._withheld:
            when = self._withheld[name]
            later = "only after the outcome" if when is None else f"at {when.isoformat()}"
            raise LookAheadError(
                f"{name!r} is not known at decision time {self.decided_at.isoformat()} "
                f"(known {later})"
            )
        raise KeyError(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self._lookup(name)
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name: str) -> Any:
        return self._lookup(name)

    def get(self, name: str, default: Any = None) -> Any:
        """Like `dict.get`, except that a withheld fact still raises `LookAheadError`."""
        try:
            return self._lookup(name)
        except KeyError:
            return default

    def __contains__(self, name: object) -> bool:
        return name in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._values))

    def known(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def withheld(self) -> tuple[str, ...]:
        return tuple(sorted(self._withheld))

    def __repr__(self) -> str:
        return f"DecisionView(at={self.decided_at.isoformat()}, known={list(self.known())})"

    def rule_view(self) -> dict[str, bool | int | str]:
        """The `scruple.domain.FIELD_TYPES` dictionary, from known facts and defaults."""
        currency = self._values["currency"]
        if currency not in FX_TO_USD_PER_100:
            raise ValueError(
                f"rules read amount_usd_minor; no conversion table for {currency} "
                f"(synthetic table covers {sorted(FX_TO_USD_PER_100)})"
            )
        return {
            "mcc": DEFAULTED_RULE_FIELDS["mcc"],
            "reason_code": self._values["reason_code"] or "unmapped",
            "amount_usd_minor": to_usd(self._values["amount_minor"], currency),
            "delivery_confirmed": DEFAULTED_RULE_FIELDS["delivery_confirmed"],
            "three_ds": DEFAULTED_RULE_FIELDS["three_ds"],
            "evidence_on_time": bool(self._values["response_allowed"]),
            "prior_dispute_on_txn": self._values["prior_disputes_on_payment"] > 0,
            "has_prior_refund": DEFAULTED_RULE_FIELDS["has_prior_refund"],
            "currency_mismatch": DEFAULTED_RULE_FIELDS["currency_mismatch"],
        }


class PaymentIndex:
    """Open times of every case per payment, to count earlier disputes."""

    def __init__(self, cases: tuple[HistoricalCase, ...]) -> None:
        self._opened: dict[str, list[tuple[datetime, str]]] = {}
        for hc in cases:
            self._opened.setdefault(hc.case.payment_ref, []).append(
                (hc.case.opened_at, hc.case.case_id)
            )

    def earlier(self, hc: HistoricalCase, at: datetime) -> int:
        return sum(
            1
            for when, cid in self._opened.get(hc.case.payment_ref, ())
            if when < at and cid != hc.case.case_id
        )


def facts_of(
    hc: HistoricalCase, index: PaymentIndex, at: datetime, merchant_account: str
) -> list[Fact]:
    """Every fact about a case with the time it became known."""
    c = hc.case
    opened = c.opened_at
    postings: list[Posting] = []
    later: list[Posting] = []
    for txn in hc.ledger:
        net = sum(ln.amount_minor for ln in txn.lines if ln.account == merchant_account)
        fee = sum(
            ln.amount_minor for ln in txn.lines if ln.account == FEE_ACCOUNT and ln.amount_minor > 0
        )
        p = Posting(occurred_at=txn.occurred_at, merchant_net_minor=net, fee_minor=fee)
        (postings if txn.occurred_at <= at else later).append(p)
    due = c.evidence_due_at
    out = [
        Fact("reason", c.source_reason, opened),
        Fact("reason_code", c.reason_code.value if c.reason_code else None, opened),
        Fact("network", c.network, opened),
        Fact("network_reason_code", c.network_reason_code, opened),
        Fact("amount_minor", c.amount_minor, opened),
        Fact("currency", c.currency, opened),
        Fact("opened_at", opened, opened),
        Fact("evidence_due_at", due, opened),
        Fact("response_allowed", due is not None and due > at, opened),
        Fact("days_to_due", (due - at).total_seconds() / 86_400 if due else None, opened),
        Fact("prior_disputes_on_payment", index.earlier(hc, at), opened),
        Fact("ledger", tuple(postings), opened),
        Fact("ledger_after_decision", tuple(later), None),
    ]
    out += [Fact(name, None, None) for name in OUTCOME_FACTS if name != "ledger_after_decision"]
    return out


def build_view(
    hc: HistoricalCase,
    index: PaymentIndex,
    at: datetime | None = None,
    merchant_account: str = "merchant",
) -> DecisionView:
    """The view at `at` (default: when the case opened)."""
    when = hc.case.opened_at if at is None else at
    if when < hc.case.opened_at:
        raise ValueError("a case cannot be decided before it opens")
    values: dict[str, Any] = {}
    withheld: dict[str, datetime | None] = {}
    for f in facts_of(hc, index, when, merchant_account):
        if f.known_at is not None and f.known_at <= when:
            values[f.name] = f.value
        else:
            withheld[f.name] = f.known_at
    return DecisionView(values, withheld, when)
