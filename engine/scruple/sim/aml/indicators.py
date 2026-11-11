"""Red-flag indicators an analyst can cite, computed only from observable data.

Each indicator is a predicate over the subject account, the accounts the analyst can
look up and a set of cited transfers. The SAR verifier (`scruple.verifiers.aml`)
recomputes every indicator an escalation or a SAR cites; the reference policy uses
the same predicates (it never reads the truth). The thresholds are ASSUMPTIONS
(docs/WORKFLOWS.md AM-2 to AM-5), except the CTR threshold (31 CFR 1010.311, M1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from enum import StrEnum

from scruple.sim.aml.models import CASH, CTR_THRESHOLD_MINOR, Account, AlertRule, Transfer

NEAR_THRESHOLD_LO_MINOR = 700_000  # AM-2: "near the threshold" means $7,000 to $10,000
NEAR_THRESHOLD_MIN_COUNT = 3
FAN_MIN_COUNTERPARTIES = 5  # AM-3
NEW_ACCOUNT_DAYS = 180  # AM-3: an account opened less than 180 days before the alert
NEW_SHARE = 0.5
PASS_THROUGH_HOURS = 72  # AM-4
PASS_THROUGH_SHARE = 0.8
CYCLE_MIN_MINOR = 500_000  # AM-4: a pass-through cycle starts with at least $5,000 in
CYCLE_SUSPICIOUS = 2
PROFILE_MULTIPLE = 3  # AM-5: inflow above three months of the declared monthly inflow


class Indicator(StrEnum):
    NEAR_THRESHOLD_CASH = "near_threshold_cash"  # >= 3 cash deposits of $7,000-$10,000
    FAN_IN = "fan_in"  # >= 5 distinct senders
    FAN_OUT = "fan_out"  # >= 5 distinct recipients
    PASS_THROUGH = "pass_through"  # noqa: S105 - >= 80% of the money in goes out within 72 hours
    NEW_COUNTERPARTIES = "new_counterparties"  # most counterparties opened < 180 days ago
    PROFILE_MISMATCH = "profile_mismatch"  # inflow far above the KYC declaration


INDICATORS: tuple[Indicator, ...] = tuple(Indicator)


def inbound(subject_id: str, txns: Iterable[Transfer]) -> list[Transfer]:
    return [t for t in txns if t.dst == subject_id]


def outbound(subject_id: str, txns: Iterable[Transfer]) -> list[Transfer]:
    return [t for t in txns if t.src == subject_id]


def near_threshold(t: Transfer) -> bool:
    return t.channel == "cash" and NEAR_THRESHOLD_LO_MINOR <= t.amount_minor <= CTR_THRESHOLD_MINOR


def reportable_cash(t: Transfer) -> bool:
    """A cash transaction of more than $10,000: the bank files a CTR (31 CFR 1010.311)."""
    return t.channel == "cash" and t.amount_minor > CTR_THRESHOLD_MINOR


def counterparties(subject_id: str, txns: Iterable[Transfer]) -> set[str]:
    return {t.src if t.dst == subject_id else t.dst for t in txns} - {subject_id, CASH}


def new_share(
    subject_id: str, accounts: Mapping[str, Account], txns: Iterable[Transfer], on: date
) -> float:
    """Share of the internal counterparties opened less than 180 days before `on`."""
    known = [
        accounts[c]
        for c in counterparties(subject_id, txns)
        if c in accounts and accounts[c].opened_on is not None
    ]
    if not known:
        return 0.0
    cutoff = on - timedelta(days=NEW_ACCOUNT_DAYS)
    return sum(a.opened_on is not None and a.opened_on > cutoff for a in known) / len(known)


def passes_through(subject_id: str, txns: Sequence[Transfer]) -> bool:
    ins, outs = inbound(subject_id, txns), outbound(subject_id, txns)
    if not ins or not outs:
        return False
    window = timedelta(hours=PASS_THROUGH_HOURS)
    timely = all(any(timedelta(0) < o.at - i.at <= window for i in ins) for o in outs)
    total_in = sum(t.amount_minor for t in ins)
    return timely and sum(t.amount_minor for t in outs) >= PASS_THROUGH_SHARE * total_in


def cycles(subject_id: str, txns: Sequence[Transfer]) -> int:
    """Inflows of at least $5,000 each followed within 72 hours by one outflow of 80% to
    100% of it (greedy, in time order, each outflow used once)."""
    used: set[str] = set()
    n = 0
    window = timedelta(hours=PASS_THROUGH_HOURS)
    outs = sorted(outbound(subject_id, txns), key=lambda t: t.at)
    for i in sorted(inbound(subject_id, txns), key=lambda t: t.at):
        if i.amount_minor < CYCLE_MIN_MINOR:
            continue
        for o in outs:
            if (
                o.txn_id not in used
                and timedelta(0) < o.at - i.at <= window
                and PASS_THROUGH_SHARE * i.amount_minor <= o.amount_minor <= i.amount_minor
            ):
                used.add(o.txn_id)
                n += 1
                break
    return n


def holds(
    indicator: Indicator,
    subject: Account,
    accounts: Mapping[str, Account],
    txns: Sequence[Transfer],
    on: date,
) -> bool:
    """Whether `indicator` holds on the cited transfers `txns` (all must involve the
    subject; callers check that)."""
    sid = subject.account_id
    match indicator:
        case Indicator.NEAR_THRESHOLD_CASH:
            deposits = [t for t in inbound(sid, txns) if near_threshold(t)]
            return len(deposits) >= NEAR_THRESHOLD_MIN_COUNT
        case Indicator.FAN_IN:
            return len({t.src for t in inbound(sid, txns)} - {CASH}) >= FAN_MIN_COUNTERPARTIES
        case Indicator.FAN_OUT:
            return len({t.dst for t in outbound(sid, txns)} - {CASH}) >= FAN_MIN_COUNTERPARTIES
        case Indicator.PASS_THROUGH:
            return passes_through(sid, txns)
        case Indicator.NEW_COUNTERPARTIES:
            return new_share(sid, accounts, txns, on) >= NEW_SHARE
        case Indicator.PROFILE_MISMATCH:
            declared = subject.expected_monthly_in_minor
            total = sum(t.amount_minor for t in inbound(sid, txns))
            return declared is not None and total > PROFILE_MULTIPLE * declared


def assess(
    rule: AlertRule,
    subject: Account,
    accounts: Mapping[str, Account],
    linked: Sequence[Transfer],
    on: date,
) -> tuple[bool, tuple[Indicator, ...]]:
    """The reference analyst's reading of an alert, from observables only: whether it
    is a typology, and the indicators that hold on the linked transfers. ASSUMPTION
    AM-6: these decision rules are ours; the generator is built so that they separate
    each typology from its lookalike."""
    sid = subject.account_id
    held = tuple(i for i in INDICATORS if holds(i, subject, accounts, linked, on))
    match rule:
        case AlertRule.CASH_NEAR_THRESHOLD:
            deposits = [t for t in inbound(sid, linked) if t.channel == "cash"]
            suspicious = Indicator.NEAR_THRESHOLD_CASH in held and not any(
                reportable_cash(t) for t in deposits
            )
        case AlertRule.FAN_IN | AlertRule.FAN_OUT:
            suspicious = Indicator.PASS_THROUGH in held and Indicator.NEW_COUNTERPARTIES in held
        case AlertRule.PASS_THROUGH:
            suspicious = cycles(sid, linked) >= CYCLE_SUSPICIOUS
    return suspicious, held


__all__ = [
    "CYCLE_SUSPICIOUS",
    "FAN_MIN_COUNTERPARTIES",
    "INDICATORS",
    "NEAR_THRESHOLD_LO_MINOR",
    "NEW_ACCOUNT_DAYS",
    "PASS_THROUGH_HOURS",
    "PROFILE_MULTIPLE",
    "Indicator",
    "assess",
    "counterparties",
    "cycles",
    "holds",
    "inbound",
    "near_threshold",
    "new_share",
    "outbound",
    "passes_through",
    "reportable_cash",
]
