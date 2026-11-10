"""Leaky audits and an imperfect verifier (docs/math/05, Theorems 5.5-5.8)."""

from __future__ import annotations

import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from scruple.stats import hacking
from scruple.stats.hacking_general import (
    LeakyAudit,
    acceptance_probability,
    best_response,
    deterrable,
    expected_gap_bound,
    expected_utility,
    leak_aware_outcomes,
    learning_optimiser,
    minimal_audit_rate,
    play_leaky_round,
    tail_bound,
)

rates = st.floats(min_value=0.0, max_value=1.0)
small = st.floats(min_value=0.0, max_value=0.45)


@settings(max_examples=150, deadline=None)
@given(
    q=st.floats(0.01, 0.6),
    beta=small,
    phi=st.floats(0.0, 0.05),
    leak=rates,
    m=st.integers(1, 120),
    gamma=st.floats(0.0, 0.7),
)
def test_leak_aware_policy_obeys_theorem_5_5(
    q: float, beta: float, phi: float, leak: float, m: int, gamma: float
) -> None:
    n = 150
    m = min(m, n)
    audit = LeakyAudit(rate=q, false_negative=beta, false_alarm=phi, leak=leak)
    pr = leak_aware_outcomes(n, m, audit)
    rho = audit.catch_floor
    tail = math.fsum(p for h, p in enumerate(pr) if h / n >= gamma - 1e-12)
    assert tail <= tail_bound(gamma, n, 1.0, rho) + 1e-12
    e_gap = math.fsum(h / n * p for h, p in enumerate(pr))
    assert e_gap <= expected_gap_bound(n, 1.0, rho) + 1e-12


def test_ignoring_the_leak_breaks_the_bound() -> None:
    n, m = 200, 109
    audit = LeakyAudit(rate=0.05, leak=0.2)
    pr = leak_aware_outcomes(n, m, audit)
    e_gap = math.fsum(h / n * p for h, p in enumerate(pr))
    assert e_gap > expected_gap_bound(n, 1.0, 0.05)  # naive rho = q
    assert e_gap <= expected_gap_bound(n, 1.0, audit.catch_floor)


@given(q=rates, beta=small)
def test_no_leak_floor_is_q_times_detection(q: float, beta: float) -> None:
    audit = LeakyAudit(rate=q, false_negative=beta)
    assert audit.catch_floor == pytest.approx(q * (1 - beta))


@given(q=st.floats(0.0, 0.999), l1=rates, l2=rates)
def test_floor_is_monotone_in_the_leak(q: float, l1: float, l2: float) -> None:
    lo, hi = sorted((l1, l2))
    assert (
        LeakyAudit(rate=q, leak=lo).catch_floor <= LeakyAudit(rate=q, leak=hi).catch_floor + 1e-15
    )
    assert LeakyAudit(rate=q, leak=0.0).catch_floor == 0.0


@given(v=st.floats(0, 1e3), c=st.floats(0, 1e3), g=st.floats(1e-3, 10), beta=small)
def test_minimal_rate_reduces_to_theorem_5_3(v: float, c: float, g: float, beta: float) -> None:
    inc = hacking.Incentive(honest_value=v, rejection_cost=c, max_gain_per_case=g)
    expected = hacking.minimal_audit_rate(inc, 1 - beta)
    assert minimal_audit_rate(v, c, g, false_negative=beta) == pytest.approx(expected, rel=1e-9)


@settings(max_examples=120, deadline=None)
@given(
    v=st.floats(0.1, 100),
    c=st.floats(0, 100),
    gs=st.lists(st.floats(0.0, 5.0), min_size=1, max_size=12),
    beta=small,
    phi=st.floats(0.0, 0.2),
    bump=st.floats(1.0, 3.0),
)
def test_no_hack_profitable_at_or_above_q_star(
    v: float, c: float, gs: list[float], beta: float, phi: float, bump: float
) -> None:
    g_max = max(max(gs), 1e-6)
    q_star = minimal_audit_rate(v, c, g_max, false_negative=beta, false_alarm=phi)
    if q_star * bump > 1:
        return
    audit = LeakyAudit(rate=q_star * bump, false_negative=beta, false_alarm=phi)
    n = 40
    honest = expected_utility(n, [], audit, v, c)
    for m in range(1, len(gs) + 1):
        assert expected_utility(n, sorted(gs, reverse=True)[:m], audit, v, c) <= honest + 1e-9


@settings(max_examples=80, deadline=None)
@given(
    v=st.floats(0.1, 100),
    c=st.floats(0, 100),
    g=st.floats(0.01, 5.0),
    beta=small,
    phi=st.floats(0, 0.2),
)
def test_one_hack_profitable_below_q_star(
    v: float, c: float, g: float, beta: float, phi: float
) -> None:
    q_star = minimal_audit_rate(v, c, g, false_negative=beta, false_alarm=phi)
    if not 0 < q_star <= 1:
        return
    audit = LeakyAudit(rate=0.9 * q_star, false_negative=beta, false_alarm=phi)
    assert expected_utility(10, [g], audit, v, c) > expected_utility(10, [], audit, v, c)


@given(
    v=st.floats(0, 10),
    c=st.floats(0, 10),
    g=st.floats(0.01, 10),
    beta=rates,
    phi=st.floats(0, 0.99),
)
def test_deterrable_iff_q_star_at_most_one(
    v: float, c: float, g: float, beta: float, phi: float
) -> None:
    q_star = minimal_audit_rate(v, c, g, false_negative=beta, false_alarm=phi)
    if abs(q_star - 1.0) < 1e-6:
        return
    assert deterrable(v, c, g, false_negative=beta, false_alarm=phi) == (q_star <= 1.0)


def test_false_alarms_raise_the_minimal_rate() -> None:
    qs = [minimal_audit_rate(1.0, 0.0, 0.1, false_alarm=phi) for phi in (0.0, 0.01, 0.1, 0.5)]
    assert qs == sorted(qs) and qs[0] < qs[-1]
    assert minimal_audit_rate(1.0, 0.0, 1.0, false_negative=1.0) == math.inf


def test_acceptance_probability_closed_form() -> None:
    audit = LeakyAudit(rate=0.1, false_negative=0.2, false_alarm=0.01)
    assert acceptance_probability(10, 3, audit) == pytest.approx(0.999**7 * 0.92**3)
    with pytest.raises(ValueError):
        acceptance_probability(3, 4, audit)


def test_best_response_switches_at_q_star() -> None:
    v, c, g = 0.449, 0.0, 0.0025
    q_star = minimal_audit_rate(v, c, g, false_negative=0.25, false_alarm=0.0019)
    lo = LeakyAudit(rate=0.5 * q_star, false_negative=0.25, false_alarm=0.0019)
    hi = LeakyAudit(rate=1.01 * q_star, false_negative=0.25, false_alarm=0.0019)
    assert best_response(200, [g] * 109, lo, v, c)[0] == 109
    assert best_response(200, [g] * 109, hi, v, c)[0] == 0


def test_simulator_matches_exact_formula() -> None:
    n, m, reps = 100, 40, 6000
    audit = LeakyAudit(rate=0.08, false_negative=0.3, false_alarm=0.01, leak=0.5)
    exact = math.fsum(leak_aware_outcomes(n, m, audit))
    rng = random.Random(3)
    acc = sum(
        play_leaky_round(n, range(m), audit, rng, leak_aware=True).accepted for _ in range(reps)
    )
    se = math.sqrt(exact * (1 - exact) / reps)
    assert abs(acc / reps - exact) < 4 * se


def test_naive_policy_acceptance_matches_closed_form() -> None:
    n, m, reps = 100, 20, 6000
    audit = LeakyAudit(rate=0.05, false_negative=0.3, false_alarm=0.02)
    exact = acceptance_probability(n, m, audit)
    rng = random.Random(4)
    acc = sum(
        play_leaky_round(n, range(m), audit, rng, leak_aware=False).accepted for _ in range(reps)
    )
    assert abs(acc / reps - exact) < 4 * math.sqrt(exact * (1 - exact) / reps)


def test_learner_stops_hacking_above_q_star() -> None:
    v, c, g = 10.0, 5.0, 1.0
    q_star = minimal_audit_rate(v, c, g, false_negative=0.3)
    audit = LeakyAudit(rate=2 * q_star, false_negative=0.3)
    _, arm = learning_optimiser(100, g, 20, audit, v, c, rounds=3000, seed=1, arms=(0, 5, 20))
    assert arm == 0


def test_bounds_edge_cases() -> None:
    assert tail_bound(0.0, 10, 1.0, 0.1) == 1.0
    assert expected_gap_bound(10, 1.0, 0.0) == 1.0
    assert expected_gap_bound(10, 1.0, 1.0) == 0.0
    with pytest.raises(ValueError):
        tail_bound(0.1, 0, 1.0, 0.1)
    with pytest.raises(ValueError):
        expected_gap_bound(10, 1.0, 1.5)
    with pytest.raises(ValueError):
        minimal_audit_rate(-1.0, 0.0, 1.0)


def test_validation() -> None:
    with pytest.raises(ValidationError):
        LeakyAudit(rate=1.5)
    with pytest.raises(ValidationError):
        LeakyAudit(rate=0.1, false_negative=0.6, false_alarm=0.5)
    with pytest.raises(ValueError):
        play_leaky_round(5, [7], LeakyAudit(rate=0.1), random.Random(0), leak_aware=True)
