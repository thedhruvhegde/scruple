"""Hypothesis strategies for the scruple domain: seeds, cases, rules, ledger postings.

Everything built here is valid by construction (it passes the Pydantic models), so
properties test behaviour, not input validation. Use `observable_views()` to feed
rules records that no generator would produce but the rule language allows.
"""

from __future__ import annotations

from hypothesis import strategies as st

from scruple.domain import FIELD_TYPES, Action, Condition, ReasonCode, RuleSpec
from scruple.sim.generator import generate_cases
from scruple.sim.ledger import Ledger
from scruple.sim.models import FX_TO_USD_PER_100, DisputeCase
from scruple.sim.policy import Policy, PolicyRule

MAX_SEED = 2**32 - 1
CURRENCIES = sorted(FX_TO_USD_PER_100)
ORDER_OPS = ("lt", "le", "gt", "ge")
EQ_OPS = ("eq", "ne")

seeds = st.integers(min_value=0, max_value=MAX_SEED)
actions = st.sampled_from(list(Action))
reason_codes = st.sampled_from([r.value for r in ReasonCode])
amounts_minor = st.integers(min_value=-(10**12), max_value=10**12)
accounts = st.from_regex(r"[a-z]{1,8}:[a-z0-9-]{1,12}", fullmatch=True)


def _value_for(field: str) -> st.SearchStrategy[bool | int | str]:
    t = FIELD_TYPES[field]
    if field == "reason_code":
        return reason_codes
    if t is bool:
        return st.booleans()
    if t is int:
        return st.integers(min_value=-(2**63), max_value=2**63 - 1)
    return st.text(max_size=8)


@st.composite
def conditions(draw: st.DrawFn) -> Condition:
    field = draw(st.sampled_from(sorted(FIELD_TYPES)))
    ops = EQ_OPS + ORDER_OPS if FIELD_TYPES[field] is int else EQ_OPS
    return Condition.model_validate(
        {"field": field, "op": draw(st.sampled_from(ops)), "value": draw(_value_for(field))}
    )


@st.composite
def rule_specs(draw: st.DrawFn) -> RuleSpec:
    return RuleSpec(
        description=draw(st.text(min_size=1, max_size=40)),
        conditions=tuple(draw(st.lists(conditions(), min_size=1, max_size=10))),
        action=draw(actions),
        priority=draw(st.integers(min_value=0, max_value=1000)),
    )


@st.composite
def policies(draw: st.DrawFn, max_rules: int = 6) -> Policy:
    specs = draw(st.lists(rule_specs(), max_size=max_rules))
    rules = tuple(
        PolicyRule(rule_id=f"r-{i:03d}", ref=f"r-{i:03d}@v1", spec=s) for i, s in enumerate(specs)
    )
    return Policy(rules=rules, default_action=draw(actions))


@st.composite
def observable_views(draw: st.DrawFn) -> dict[str, bool | int | str]:
    """Any record the rule language can see, including ones no generator makes."""
    return {f: draw(_value_for(f)) for f in sorted(FIELD_TYPES)}


@st.composite
def case_batches(draw: st.DrawFn, max_cases: int = 60) -> list[DisputeCase]:
    return generate_cases(draw(seeds), draw(st.integers(min_value=1, max_value=max_cases)))


@st.composite
def balanced_postings(draw: st.DrawFn) -> list[tuple[str, int]]:
    """2-6 postings that sum to zero (the last one balances the rest)."""
    accts = draw(st.lists(accounts, min_size=2, max_size=6))
    amounts = [draw(amounts_minor) for _ in accts[:-1]]
    return [*zip(accts[:-1], amounts, strict=True), (accts[-1], -sum(amounts))]


@st.composite
def unbalanced_postings(draw: st.DrawFn) -> list[tuple[str, int]]:
    postings = draw(balanced_postings())
    skew = draw(amounts_minor.filter(lambda n: n != 0))
    acct, amt = postings[-1]
    return [*postings[:-1], (acct, amt + skew)]


@st.composite
def journal_entries(draw: st.DrawFn) -> tuple[str, str, list[tuple[str, int]]]:
    """(kind, currency, postings) for `Ledger.post`."""
    kind = draw(st.sampled_from(["capture", "refund", "chargeback_debit", "fee"]))
    return kind, draw(st.sampled_from(CURRENCIES)), draw(balanced_postings())


def ledger_from(entries: list[tuple[str, str, list[tuple[str, int]]]]) -> Ledger:
    ledger = Ledger()
    for i, (kind, ccy, postings) in enumerate(entries):
        ledger.post(kind, f"t-{i}", None, ccy, postings)
    return ledger
