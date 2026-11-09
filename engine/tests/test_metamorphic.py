"""Metamorphic tests (roadmap 163): change the input in a way whose effect on the
output is known, and check the output changed exactly that way.

- scaling every amount by k scales debits and costs by k (within FX rounding, fees
  fixed), and scales the paired bootstrap interval by k;
- permuting case order changes no per-case result and no aggregate (regret, slices,
  accuracy, ledger findings), and not the bootstrap interval either;
- relabelling case and transaction ids changes nothing but the labels;
- adding a dominated action (escalation, which costs the optimum plus analyst time)
  never improves the optimum, and adding a no-op (a rule that never matches, a
  RequestInfo call) changes no outcome.

The sequential e-value tests are deliberately order-dependent (they bet in arrival
order) and the canary split hashes case ids by design, so neither is covered here.
All data is synthetic.
"""

from __future__ import annotations

import random
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.domain import FIELD_TYPES, Action, Condition, RuleSpec
from scruple.gate.stats import paired_bootstrap_ci
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    DEFAULT_MIX,
    FULL_MIX,
    Accept,
    AgentAction,
    CardDispute,
    DisputeEngine,
    Represent,
    RequestInfo,
    drain,
)
from scruple.sim.disputes import generate_cases as generate_disputes
from scruple.sim.engine import RunResult, Simulator, cost_usd, oracle, outcome_for
from scruple.sim.generator import generate_cases
from scruple.sim.models import ESCALATION_COST_USD, CaseResult, DisputeCase
from scruple.sim.policy import Policy, PolicyRule
from scruple.verifiers.ledger import verify_ledger
from scruple.verifiers.outcome import score_outcomes
from tests.strategies import conditions, seeds

AMOUNT_FIELDS = {"amount_usd_minor"}
NO_ESCALATE = st.sampled_from([Action.ACCEPT, Action.CONTEST])


@st.composite
def amount_blind_policies(draw: st.DrawFn, max_rules: int = 5) -> Policy:
    """Policies whose rules never read an amount, so scaling cannot change a decision.
    Only ACCEPT and CONTEST: an escalation resolves at the hindsight optimum, whose
    choice depends on the (fixed, unscaled) fees."""
    rules = []
    for i in range(draw(st.integers(0, max_rules))):
        conds = draw(
            st.lists(conditions().filter(lambda c: c.field not in AMOUNT_FIELDS), min_size=1)
        )
        spec = RuleSpec(
            description=f"r{i}",
            conditions=tuple(conds[:10]),
            action=draw(NO_ESCALATE),
            priority=draw(st.integers(0, 1000)),
        )
        rules.append(PolicyRule(rule_id=f"r-{i}", ref=f"r-{i}@v1", spec=spec))
    return Policy(rules=tuple(rules), default_action=draw(NO_ESCALATE))


@st.composite
def any_policies(draw: st.DrawFn) -> Policy:
    from tests.strategies import policies

    return draw(policies(max_rules=5))


def scaled(c: DisputeCase, k: int) -> DisputeCase:
    return c.model_copy(
        update={
            "amount_minor": c.amount_minor * k,
            "captured_minor": c.captured_minor * k,
            "refunded_minor": c.refunded_minor * k,
        }
    )


def relabelled(cases: list[DisputeCase]) -> tuple[list[DisputeCase], dict[str, str]]:
    """New case and txn ids (a bijection that keeps shared transactions shared)."""
    txn = {t: f"zz-txn-{i:05d}" for i, t in enumerate(sorted({c.txn_id for c in cases}))}
    cid = {c.case_id: f"zz-case-{i:05d}" for i, c in enumerate(reversed(cases))}
    return [
        c.model_copy(update={"case_id": cid[c.case_id], "txn_id": txn[c.txn_id]}) for c in cases
    ], cid


def strip(r: CaseResult) -> tuple[object, ...]:
    d = r.model_dump()
    d.pop("case_id")
    d.pop("txn_id")
    return tuple(sorted(d.items()))


def finding_kinds(cases: list[DisputeCase], run: RunResult) -> Counter[str]:
    return Counter(f.kind for f in verify_ledger(cases, run).findings)


# --------------------------------------------------------------------------- scaling
@settings(max_examples=40, deadline=None)
@given(seeds, st.integers(1, 40), st.integers(2, 50), amount_blind_policies())
def test_scaling_amounts_scales_debits_and_costs(seed: int, n: int, k: int, p: Policy) -> None:
    cases = generate_cases(seed, n)
    big = [scaled(c, k) for c in cases]
    r1, rk = Simulator(cases).run(p).results, Simulator(big).run(p).results
    for c, a, b in zip(cases, r1, rk, strict=True):
        assert (a.action, a.resolution, a.stages) == (b.action, b.resolution, b.stages)
        assert (a.fee_usd_minor, a.ops_usd_minor) == (b.fee_usd_minor, b.ops_usd_minor)
        fixed = a.fee_usd_minor + a.ops_usd_minor
        if c.txn_currency == c.dispute_currency == "USD":
            assert b.debit_txn_minor == k * a.debit_txn_minor
            assert b.cost_usd_minor - fixed == k * (a.cost_usd_minor - fixed)
        else:
            # integer FX floors: each conversion is off by less than k minor units
            assert abs(b.debit_txn_minor - k * a.debit_txn_minor) <= 2 * k
            assert abs((b.cost_usd_minor - fixed) - k * (a.cost_usd_minor - fixed)) <= 4 * k


@given(seeds, st.integers(1, 40), st.integers(2, 50))
def test_scaling_never_changes_which_action_is_liable(seed: int, n: int, k: int) -> None:
    for c in generate_cases(seed, n):
        for action in (Action.ACCEPT, Action.CONTEST):
            o1, ok = outcome_for(c, action), outcome_for(scaled(c, k), action)
            assert o1.resolution is ok.resolution and o1.stages == ok.stages
            assert (o1.debit_txn_minor == 0) == (ok.debit_txn_minor == 0)


@given(
    st.lists(st.integers(-(10**6), 10**6), max_size=300),
    st.integers(2, 1_000),
    st.integers(0, 2**32),
)
def test_scaling_differences_scales_the_bootstrap_interval(
    diffs: list[int], k: int, seed: int
) -> None:
    m1, lo1, hi1 = paired_bootstrap_ci(diffs, 200, 0.95, seed)
    mk, lok, hik = paired_bootstrap_ci([k * d for d in diffs], 200, 0.95, seed)
    for a, b in ((m1, mk), (lo1, lok), (hi1, hik)):
        assert b == pytest.approx(k * a, rel=1e-9, abs=1e-6)


# ------------------------------------------------------------------------ permutation
@settings(max_examples=40, deadline=None)
@given(seeds, st.integers(1, 60), any_policies(), st.randoms(use_true_random=False))
def test_permuting_cases_changes_no_result_or_aggregate(
    seed: int, n: int, p: Policy, rnd: random.Random
) -> None:
    cases = generate_cases(seed, n)
    shuffled = list(cases)
    rnd.shuffle(shuffled)
    a, b = Simulator(cases).run(p), Simulator(shuffled).run(p)
    assert {r.case_id: r for r in a.results} == {r.case_id: r for r in b.results}
    sa, sb = score_outcomes(cases, a), score_outcomes(shuffled, b)
    assert sa.regret == sb.regret
    assert sa.mean_regret == sb.mean_regret and sa.accuracy == sb.accuracy
    assert sa.escalation_rate == sb.escalation_rate
    assert sa.slice_mean_regret == pytest.approx(sb.slice_mean_regret)
    assert finding_kinds(cases, a) == finding_kinds(shuffled, b)
    # the journal holds the same postings, only in a different order
    key = lambda e: (e.kind, e.txn_id, e.case_id, e.currency, e.postings)  # noqa: E731
    assert sorted(map(key, a.journal), key=repr) == sorted(map(key, b.journal), key=repr)


@settings(max_examples=60)
@given(st.lists(st.integers(-(10**5), 10**5), max_size=200), st.randoms(use_true_random=False))
def test_permuting_differences_does_not_move_the_bootstrap_interval(
    diffs: list[int], rnd: random.Random
) -> None:
    shuffled = list(diffs)
    rnd.shuffle(shuffled)
    assert paired_bootstrap_ci(diffs, 300, 0.95, 7) == pytest.approx(
        paired_bootstrap_ci(shuffled, 300, 0.95, 7)
    )


# ------------------------------------------------------------------------- relabelling
@settings(max_examples=40, deadline=None)
@given(seeds, st.integers(1, 60), any_policies())
def test_relabelling_ids_changes_only_the_labels(seed: int, n: int, p: Policy) -> None:
    cases = generate_cases(seed, n)
    renamed, mapping = relabelled(cases)
    a, b = Simulator(cases).run(p), Simulator(renamed).run(p)
    assert [strip(r) for r in a.results] == [strip(r) for r in b.results]
    assert [mapping[r.case_id] for r in a.results] == [r.case_id for r in b.results]
    sa, sb = score_outcomes(cases, a), score_outcomes(renamed, b)
    assert {mapping[k]: v for k, v in sa.regret.items()} == sb.regret
    assert (sa.mean_regret, sa.accuracy) == (sb.mean_regret, sb.accuracy)
    assert finding_kinds(cases, a) == finding_kinds(renamed, b)


def _engine_outcomes(
    cases: list[CardDispute], plan: list[tuple[int, AgentAction]]
) -> list[tuple[object, ...]]:
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    e = DisputeEngine(cases, clock)
    for i, action in plan:
        e.act(cases[i % len(cases)].case_id, action)
    drain(e, clock)
    out: list[tuple[object, ...]] = []
    for c in cases:
        o = e.outcome(c.case_id)
        assert o is not None
        out.append((o.resolution, o.path, o.cardholder_net_minor, o.fees_usd_minor, o.correct))
    return out


@settings(max_examples=25, deadline=None)
@given(
    seeds,
    st.integers(1, 6),
    st.lists(st.tuples(st.integers(0, 5), st.sampled_from([Accept(), Represent()])), max_size=6),
)
def test_relabelling_dispute_case_ids_keeps_engine_outcomes(
    seed: int, n: int, plan: list[tuple[int, AgentAction]]
) -> None:
    """Case ids are not observable features: the issuer and the engine must not
    depend on them (the issuer is seeded from case content, not the label)."""
    cases = generate_disputes(seed, n, DEFAULT_MIX)
    renamed = [c.model_copy(update={"case_id": f"renamed-{i}"}) for i, c in enumerate(cases)]
    if any(c.documents for c in cases):
        return  # documents carry the case id in their own ids; covered by evidence tests
    assert _engine_outcomes(cases, plan) == _engine_outcomes(renamed, plan)


# --------------------------------------------------------------- dominated actions
@given(seeds, st.integers(1, 80))
def test_a_dominated_action_never_improves_the_optimum(seed: int, n: int) -> None:
    for c in generate_cases(seed, n):
        action, _, cost = oracle(c)
        costs = {a: cost_usd(c, outcome_for(c, a)) for a in (Action.ACCEPT, Action.CONTEST)}
        assert cost == min(costs.values())
        assert costs[action] == cost
        # escalation resolves at the optimum but adds analyst time: dominated
        escalate = cost + ESCALATION_COST_USD
        assert min(*costs.values(), escalate) == cost


@settings(max_examples=40, deadline=None)
@given(seeds, st.integers(1, 60), any_policies(), st.sampled_from(sorted(FIELD_TYPES)))
def test_adding_a_rule_that_never_matches_changes_nothing(
    seed: int, n: int, p: Policy, field: str
) -> None:
    impossible = (
        Condition(field="amount_usd_minor", op="lt", value=0),
        Condition(field="amount_usd_minor", op="gt", value=0),
    )
    never = RuleSpec(description="never", conditions=impossible, action=Action.ESCALATE, priority=0)
    p2 = p.with_rule(PolicyRule(rule_id="zz-never", ref="zz-never@v1", spec=never))
    cases = generate_cases(seed, n)
    a, b = Simulator(cases).run(p), Simulator(cases).run(p2)
    assert a.digest() == b.digest()


@settings(max_examples=25, deadline=None)
@given(
    seeds,
    st.integers(1, 6),
    st.booleans(),
    st.lists(st.tuples(st.integers(0, 5), st.sampled_from([Accept(), Represent()])), max_size=6),
    st.lists(st.integers(0, 6), max_size=4),
)
def test_inserting_request_info_changes_no_outcome(
    seed: int, n: int, full: bool, plan: list[tuple[int, AgentAction]], at: list[int]
) -> None:
    """RequestInfo moves no money and no stage, so an agent that asks first ends up
    with exactly the same outcomes as one that does not."""
    cases = generate_disputes(seed, n, FULL_MIX if full else DEFAULT_MIX)
    noisy = list(plan)
    for j in sorted(at, reverse=True):
        noisy.insert(min(j, len(noisy)), (j, RequestInfo()))
    assert _engine_outcomes(cases, plan) == _engine_outcomes(cases, noisy)
