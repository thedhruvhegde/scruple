"""Property tests. Strategies live in tests/strategies.py; profiles in conftest.py."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.domain import FIELD_TYPES, Action, RuleSpec
from scruple.hashing import content_hash
from scruple.sim import Policy, Simulator, generate_cases
from scruple.sim.ledger import Ledger, UnbalancedEntryError
from scruple.sim.models import DisputeCase
from scruple.verifiers import verify_ledger
from tests import strategies as s

LEDGER_KINDS = {"unbalanced_entry", "conservation", "result_ledger_mismatch", "unknown_case"}


def _net_by_currency(
    ledger: Ledger, entries: list[tuple[str, str, list[tuple[str, int]]]]
) -> dict[str, int]:
    accounts = {(a, ccy) for _, ccy, ps in entries for a, _ in ps}
    out: dict[str, int] = {}
    for a, ccy in accounts:
        out[ccy] = out.get(ccy, 0) + ledger.balance(a, ccy)
    return out


# --- ledger -----------------------------------------------------------------------


@given(st.lists(s.journal_entries(), max_size=30))
def test_ledger_balances_after_any_sequence_of_postings(
    entries: list[tuple[str, str, list[tuple[str, int]]]],
) -> None:
    ledger = s.ledger_from(entries)
    assert all(sum(p.amount_minor for p in e.postings) == 0 for e in ledger.entries)
    assert all(net == 0 for net in _net_by_currency(ledger, entries).values())
    restored = Ledger.restore(ledger.snapshot())
    for _, ccy, ps in entries:
        for a, _ in ps:
            assert restored.balance(a, ccy) == ledger.balance(a, ccy)


@given(st.lists(s.journal_entries(), max_size=10), s.unbalanced_postings())
def test_unbalanced_entry_is_refused_and_changes_nothing(
    entries: list[tuple[str, str, list[tuple[str, int]]]], bad: list[tuple[str, int]]
) -> None:
    ledger = s.ledger_from(entries)
    before = ledger.snapshot()
    with pytest.raises(UnbalancedEntryError):
        ledger.post("fee", "t-bad", None, "USD", bad)
    assert ledger.snapshot() == before


@given(s.case_batches(max_cases=40), st.lists(s.actions, max_size=40))
def test_ledger_balances_after_any_sequence_of_simulated_actions(
    cases: list[DisputeCase], acts: list[Action]
) -> None:
    sim = Simulator(cases)
    for a in acts:
        if sim.done:
            break
        sim.step(Policy(rules=(), default_action=a))
    run = sim.result()
    assert all(sum(p.amount_minor for p in e.postings) == 0 for e in run.journal)
    kinds = {f.kind for f in verify_ledger(cases, run).findings}
    assert not kinds & LEDGER_KINDS, kinds


@given(s.case_batches(max_cases=40), s.policies())
def test_ledger_balances_under_any_learned_policy(cases: list[DisputeCase], p: Policy) -> None:
    run = Simulator(cases).run(p)
    kinds = {f.kind for f in verify_ledger(cases, run).findings}
    assert not kinds & LEDGER_KINDS, kinds
    assert len(run.results) == len(cases)


# --- generator ----------------------------------------------------------------------


@given(s.seeds, st.integers(min_value=1, max_value=80))
def test_generator_is_deterministic_for_any_seed(seed: int, n: int) -> None:
    a, b = generate_cases(seed, n), generate_cases(seed, n)
    assert a == b
    assert content_hash([c.model_dump(mode="json") for c in a]) == content_hash(
        [c.model_dump(mode="json") for c in b]
    )
    assert len(a) == n
    assert len({c.case_id for c in a}) == n


@given(s.seeds, st.integers(min_value=1, max_value=60), s.policies())
def test_simulation_is_deterministic_for_any_seed_and_policy(seed: int, n: int, p: Policy) -> None:
    cases = generate_cases(seed, n)
    assert Simulator(cases).run(p).digest() == Simulator(cases).run(p).digest()


@given(s.case_batches(max_cases=30))
def test_generated_cases_expose_exactly_the_observable_fields(cases: list[DisputeCase]) -> None:
    for c in cases:
        view = c.view()
        assert set(view) == set(FIELD_TYPES)
        for field, value in view.items():
            assert type(value) is FIELD_TYPES[field]


# --- rule evaluation ----------------------------------------------------------------


@given(s.rule_specs(), s.observable_views())
def test_rule_evaluation_never_raises_on_any_observable_record(
    spec: RuleSpec, view: dict[str, bool | int | str]
) -> None:
    assert isinstance(spec.matches(view), bool)
    assert spec.matches(view) == all(c.matches(view) for c in spec.conditions)


@given(s.policies(), s.observable_views())
def test_policy_always_decides_and_names_who_decided(
    p: Policy, view: dict[str, bool | int | str]
) -> None:
    action, ref = p.decide(view)
    assert action in set(Action)
    assert ref == "default" or ref in p.refs()
    if ref != "default":
        winner = next(r for r in p.rules if r.ref == ref)
        assert winner.spec.matches(view) and winner.spec.action is action
        earlier = p.rules[: p.rules.index(winner)]
        assert not any(r.spec.matches(view) for r in earlier)


@given(s.rule_specs())
def test_rule_specs_round_trip_through_json_with_a_stable_hash(spec: RuleSpec) -> None:
    again = RuleSpec.model_validate_json(spec.model_dump_json())
    assert again == spec and content_hash(again) == content_hash(spec)


# --- slow, broader runs (make test-slow) ---------------------------------------------


@pytest.mark.slow
@settings(max_examples=500)
@given(s.case_batches(max_cases=200), s.policies(max_rules=10))
def test_slow_ledger_balances_on_large_batches(cases: list[DisputeCase], p: Policy) -> None:
    run = Simulator(cases).run(p)
    kinds = {f.kind for f in verify_ledger(cases, run).findings}
    assert not kinds & LEDGER_KINDS, kinds


def test_ci_profile_is_derandomized() -> None:
    ci = settings.get_profile("ci")
    assert ci.derandomize is True and ci.deadline is None
