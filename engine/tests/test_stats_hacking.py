"""Hacking-gap bounds under spot-check audits: properties and adversarial simulation."""

from __future__ import annotations

import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats.hacking import (
    AuditModel,
    Incentive,
    available_inflation,
    best_response,
    expected_gap_bound,
    expected_utility,
    gap_tail_bound,
    hack_all,
    hack_largest,
    hack_random,
    honest,
    learning_optimiser,
    minimal_audit_rate,
    play_round,
    under_tolerance,
    undetected_probability,
)

money = st.floats(min_value=0.0, max_value=1e4)
gains = st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=30)


@given(v=money, c=money, gs=gains, d=st.floats(0.05, 1.0), bump=st.floats(1.0, 3.0))
def test_no_hack_profitable_at_minimal_rate(
    v: float, c: float, gs: list[float], d: float, bump: float
) -> None:
    g_max = max(max(gs), 1e-6)
    inc = Incentive(honest_value=v, rejection_cost=c, max_gain_per_case=g_max)
    q = minimal_audit_rate(inc, d) * bump
    if q > 1:
        return
    audit = AuditModel(rate=q, detection=d)
    m, eu = best_response(gs, audit, inc)
    assert m == 0
    assert eu == pytest.approx(v)
    for k in range(1, len(gs) + 1):  # every subset size, best subset first
        assert expected_utility(sorted(gs, reverse=True)[:k], audit, inc) <= v + 1e-9 * (1 + v)


@given(v=money, c=money, g=st.floats(0.01, 10.0), shrink=st.floats(0.0, 0.95))
def test_hacking_profitable_below_minimal_rate(v: float, c: float, g: float, shrink: float) -> None:
    inc = Incentive(honest_value=v, rejection_cost=c, max_gain_per_case=g)
    q = minimal_audit_rate(inc) * shrink
    m, eu = best_response([g], AuditModel(rate=q), inc)
    assert m == 1 and eu > v


@given(q=st.floats(0, 1), n=st.integers(1, 500), gamma=st.floats(0, 1), eps=st.floats(0.01, 1))
def test_tail_bound_monotone(q: float, n: int, gamma: float, eps: float) -> None:
    b = gap_tail_bound(gamma, n, eps, AuditModel(rate=q))
    assert 0.0 <= b <= 1.0
    assert gap_tail_bound(gamma, n, eps, AuditModel(rate=min(1.0, q + 0.1))) <= b + 1e-15
    assert gap_tail_bound(gamma, n + 10, eps, AuditModel(rate=q)) <= b + 1e-15
    assert gap_tail_bound(min(gamma + 0.1, 1.0), n, eps, AuditModel(rate=q)) <= b + 1e-15


@given(q=st.floats(0.001, 0.999), n=st.integers(1, 1000), eps=st.floats(0.01, 1))
def test_expected_gap_bound_is_sup_over_m(q: float, n: int, eps: float) -> None:
    audit = AuditModel(rate=q)
    bound = expected_gap_bound(n, eps, audit)
    best = max(m * eps / n * (1 - q) ** m for m in range(0, n + 1))
    assert best <= bound + 1e-12


def test_bound_edge_cases_and_validation() -> None:
    assert expected_gap_bound(10, 1.0, AuditModel(rate=0.0)) == 1.0
    assert expected_gap_bound(10, 1.0, AuditModel(rate=1.0)) == 0.0
    assert gap_tail_bound(0.0, 10, 1.0, AuditModel(rate=0.5, tolerance=0.1)) == 1.0
    assert undetected_probability(0, AuditModel(rate=0.5)) == 1.0
    with pytest.raises(ValueError):
        undetected_probability(-1, AuditModel(rate=0.5))
    with pytest.raises(ValueError):
        gap_tail_bound(0.5, 0, 1.0, AuditModel(rate=0.5))
    with pytest.raises(ValueError):
        expected_gap_bound(1, 0.0, AuditModel(rate=0.5))
    with pytest.raises(ValueError):
        minimal_audit_rate(Incentive(honest_value=1, rejection_cost=1, max_gain_per_case=1), 0.0)
    with pytest.raises(ValueError):
        best_response(
            [-1.0],
            AuditModel(rate=0.5),
            Incentive(honest_value=1, rejection_cost=1, max_gain_per_case=1),
        )
    with pytest.raises(ValidationError):
        AuditModel(rate=1.5)
    with pytest.raises(ValueError, match="policy must"):
        play_round(lambda e, r: [2.0] * len(e), [1.0], AuditModel(rate=0.5), random.Random(0))


def test_free_resubmission_needs_full_audit() -> None:
    inc = Incentive(honest_value=0.0, rejection_cost=0.0, max_gain_per_case=0.3)
    assert minimal_audit_rate(inc) == pytest.approx(1.0)


def test_under_tolerance_policy_is_invisible_but_bounded() -> None:
    rng = random.Random(1)
    audit = AuditModel(rate=1.0, tolerance=0.02)
    eps = available_inflation(500, 0.5, 1.0, rng)
    res = play_round(under_tolerance(0.02), eps, audit, rng)
    assert res.accepted and res.gap <= 0.02 and res.hacked == 0
    assert play_round(honest(), eps, audit, rng).accepted


# --- Monte Carlo ------------------------------------------------------------------------


def _tail_check(reps: int, seed: int) -> None:
    n, eps_max, q = 200, 1.0, 0.05
    audit = AuditModel(rate=q)
    rng = random.Random(seed)
    eps = available_inflation(n, 0.5, eps_max, rng)
    for policy in (hack_all(), hack_random(5), hack_random(20), hack_largest(10), hack_largest(60)):
        results = [play_round(policy, eps, audit, rng) for _ in range(reps)]
        vals = [r.gap if r.accepted else 0.0 for r in results]
        mean = sum(vals) / reps
        se = math.sqrt(sum((v - mean) ** 2 for v in vals) / reps / reps)
        assert mean <= expected_gap_bound(n, eps_max, audit) + 3 * se + 1e-12
        for gamma in (0.005, 0.01, 0.02, 0.05):
            freq = sum(1 for r in results if r.accepted and r.gap >= gamma) / reps
            bound = gap_tail_bound(gamma, n, eps_max, audit)
            assert freq <= bound + 3 * math.sqrt(max(bound * (1 - bound), 1e-4) / reps)


def test_tail_and_expectation_bounds_fast() -> None:
    _tail_check(300, 2)


@pytest.mark.slow
def test_tail_and_expectation_bounds_slow() -> None:
    _tail_check(5000, 22)


@pytest.mark.parametrize("m", [0, 1, 5, 20])
def test_expected_utility_matches_simulation(m: int) -> None:
    rng = random.Random(10 + m)
    eps = available_inflation(300, 0.5, 1.0, rng)
    audit = AuditModel(rate=0.03, detection=0.8)
    inc = Incentive(honest_value=5.0, rejection_cost=2.0, max_gain_per_case=1.0)
    reps = 3000
    pay = 0.0
    for _ in range(reps):
        r = play_round(hack_largest(m), eps, audit, rng)
        pay += inc.honest_value + r.proxy_gain if r.accepted else -inc.rejection_cost
    predicted = expected_utility(sorted(eps, reverse=True)[:m], audit, inc)
    assert pay / reps == pytest.approx(predicted, abs=4 * (inc.honest_value + 25) / math.sqrt(reps))


@pytest.mark.parametrize(("factor", "profitable"), [(0.1, True), (1.5, False)])
def test_learning_optimiser_follows_theory(factor: float, profitable: bool) -> None:
    rng = random.Random(4)
    eps = available_inflation(200, 0.3, 1.0, rng)
    inc = Incentive(honest_value=10.0, rejection_cost=5.0, max_gain_per_case=1.0)
    q = minimal_audit_rate(inc) * factor
    mean_pay, arm = learning_optimiser(
        eps, AuditModel(rate=q), inc, rounds=1500, arms=(0, 1, 3, 10, 30), seed=9
    )
    if profitable:
        assert mean_pay > inc.honest_value and arm > 0
    else:
        assert mean_pay <= inc.honest_value + 0.5 and arm == 0
