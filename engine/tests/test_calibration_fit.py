"""Calibration fit (153): parameter recovery from aggregates of synthetic histories with
known parameters, fallbacks, divergence before and after, and the Stripe rendering.

All histories here are SYNTHETIC (scruple.calibration.synthetic)."""

from __future__ import annotations

import inspect
import math
import typing

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.calibration import (
    DEFAULT_PROFILE,
    OTHER,
    CalibrationConfig,
    CalibrationProfile,
    HistoryAggregates,
    ReasonProfile,
    calibrate,
    compute_aggregates,
    divergence,
    fit_profile,
    generate_history,
    stripe_disputes,
    to_case_mix,
)
from scruple.calibration.synthetic import SYNTHETIC_METADATA
from scruple.ingest.models import ExternalCase, LedgerMovement
from scruple.ingest.stripe import ingest_dispute
from scruple.sim.disputes import generate_cases
from scruple.sim.networks import Network


def truth_profile(
    p_contest_a: float = 0.6,
    p_contest_b: float = 0.3,
    p_with: float = 0.65,
    p_without: float = 0.15,
    p_key: float = 0.5,
    p_win_b: float = 0.4,
    mu: float = 8.6,
    sd: float = 1.0,
    p_inquiry: float = 0.15,
    p_late: float = 0.1,
) -> CalibrationProfile:
    return CalibrationProfile(
        kind="default",
        network_weights={"mastercard": 0.35, "visa": 0.65},
        reasons={
            "fraudulent": ReasonProfile(
                weight=0.6,
                p_contest=p_contest_a,
                key_evidence="customer_purchase_ip",
                p_key_evidence=p_key,
                p_win_with_key=p_with,
                p_win_without_key=p_without,
            ),
            "product_not_received": ReasonProfile(
                weight=0.4, p_contest=p_contest_b, p_win_with_key=p_win_b, p_win_without_key=p_win_b
            ),
        },
        p_inquiry=p_inquiry,
        p_open=0.05,
        p_late=p_late,
        amount_log_mean=mu,
        amount_log_sd=sd,
        dispute_fee_minor=1_500,
    )


@settings(max_examples=8)
@given(
    seed=st.integers(0, 10_000),
    p_contest_a=st.floats(0.35, 0.85),
    p_contest_b=st.floats(0.3, 0.7),
    p_with=st.floats(0.55, 0.85),
    p_without=st.floats(0.05, 0.3),
    p_key=st.floats(0.3, 0.7),
    p_win_b=st.floats(0.2, 0.7),
    mu=st.floats(7.0, 10.0),
    sd=st.floats(0.5, 1.5),
    p_inquiry=st.floats(0.05, 0.4),
    p_late=st.floats(0.05, 0.3),
)
def test_fit_recovers_known_parameters(
    seed: int,
    p_contest_a: float,
    p_contest_b: float,
    p_with: float,
    p_without: float,
    p_key: float,
    p_win_b: float,
    mu: float,
    sd: float,
    p_inquiry: float,
    p_late: float,
) -> None:
    truth = truth_profile(
        p_contest_a, p_contest_b, p_with, p_without, p_key, p_win_b, mu, sd, p_inquiry, p_late
    )
    h = generate_history(truth, 8_000, seed)
    fit = fit_profile(compute_aggregates(h.cases, h.movements))
    # Tolerances are about five standard errors at these sample sizes (n = 8,000).
    assert fit.fallbacks == ()
    a, b = fit.reasons["fraudulent"], fit.reasons["product_not_received"]
    assert math.isclose(a.weight, 0.6, abs_tol=0.04)
    assert math.isclose(fit.p_inquiry, p_inquiry, abs_tol=0.03)
    assert math.isclose(fit.p_late, p_late, abs_tol=0.05)
    assert math.isclose(a.p_contest, p_contest_a, abs_tol=0.05)
    assert math.isclose(b.p_contest, p_contest_b, abs_tol=0.06)
    assert a.key_evidence == "customer_purchase_ip"
    assert math.isclose(a.p_key_evidence, p_key, abs_tol=0.08)
    assert math.isclose(a.p_win_with_key, p_with, abs_tol=0.1)
    assert math.isclose(a.p_win_without_key, p_without, abs_tol=0.1)
    assert math.isclose(b.p_win, p_win_b, abs_tol=0.1)
    assert math.isclose(fit.amount_log_mean, mu, abs_tol=0.06)
    assert math.isclose(fit.amount_log_sd, sd, abs_tol=0.06)
    assert fit.dispute_fee_minor == 1_500
    assert math.isclose(fit.network_weights["visa"], 0.65, abs_tol=0.04)


def test_noise_field_is_not_taken_as_the_key() -> None:
    h = generate_history(truth_profile(), 8_000, 3)
    fit = fit_profile(compute_aggregates(h.cases, h.movements))
    assert fit.reasons["product_not_received"].key_evidence is None


@settings(max_examples=5)
@given(st.integers(0, 10_000))
def test_calibration_reduces_divergence(seed: int) -> None:
    h = generate_history(truth_profile(mu=10.0, p_inquiry=0.35), 4_000, seed)
    r = calibrate(h.cases, h.movements, n_synthetic=4_000, seed=seed + 1)
    assert r.improved
    assert r.after.overall < r.before.overall / 3
    assert r.comparison_label.startswith("SYNTHETIC")


def test_divergence_of_identical_aggregates_is_zero() -> None:
    h = generate_history(truth_profile(), 2_000, 1)
    a = compute_aggregates(h.cases, h.movements)
    d = divergence(a, a)
    assert d.overall == 0 and all(v == 0 for v in d.components.values())


def test_fit_takes_aggregates_only() -> None:
    hints = typing.get_type_hints(fit_profile)
    params = list(inspect.signature(fit_profile).parameters)
    assert params == ["aggs", "prior"]
    assert hints["aggs"] is HistoryAggregates
    with pytest.raises(AttributeError):
        fit_profile(list(generate_history(truth_profile(), 50, 1).cases))  # type: ignore[arg-type]


def test_fit_is_deterministic_and_survives_json() -> None:
    h = generate_history(truth_profile(), 3_000, 9)
    a = compute_aggregates(h.cases, h.movements)
    again = HistoryAggregates.model_validate_json(a.model_dump_json())
    assert fit_profile(again) == fit_profile(a)
    assert fit_profile(a).model_dump_json() == fit_profile(a).model_dump_json()


def test_small_history_falls_back_and_pools_rare_reasons() -> None:
    h = generate_history(truth_profile(), 60, 2)
    a = compute_aggregates(h.cases, h.movements, CalibrationConfig(k=10))
    fit = fit_profile(a)
    assert fit.fallbacks  # not enough cases for every rate
    for path, v in a.cells():
        assert v == 0 or v >= 10, path
    assert math.isclose(sum(r.weight for r in fit.reasons.values()), 1.0)
    # an unknown reason uses OTHER when present, else pooled rates
    r = fit.reason("never_seen")
    assert 0 <= r.p_win <= 1


def test_high_k_withholds_and_falls_back_to_prior() -> None:
    h = generate_history(truth_profile(), 300, 4)
    a = compute_aggregates(h.cases, h.movements, CalibrationConfig(k=250))
    fit = fit_profile(a)
    assert "p_contest (pooled)" in fit.fallbacks and "amounts" not in fit.fallbacks
    assert set(fit.reasons) <= {"fraudulent", OTHER}


def test_win_interval_and_probability() -> None:
    h = generate_history(truth_profile(), 5_000, 5)
    fit = fit_profile(compute_aggregates(h.cases, h.movements))
    a = fit.reasons["fraudulent"]
    lo, hi = a.win_interval()
    assert lo < a.p_win < hi
    assert fit.win_probability("fraudulent", ("customer_purchase_ip",)) == a.p_win_with_key
    assert fit.win_probability("fraudulent", ()) == a.p_win_without_key
    assert fit.win_probability("fraudulent") == a.p_win
    assert DEFAULT_PROFILE.reasons["general"].win_interval() == (0.0, 1.0)


def test_profile_validation() -> None:
    with pytest.raises(ValueError):
        ReasonProfile(weight=1, p_contest=0.5, p_win_with_key=0.4, p_win_without_key=0.3)
    with pytest.raises(ValueError):
        truth_profile().model_copy(update={}).model_validate(
            {**truth_profile().model_dump(), "network_weights": {"visa": 0.5}}
        )


def test_generator_is_deterministic_and_seeded() -> None:
    p = truth_profile()
    assert generate_history(p, 200, 1) == generate_history(p, 200, 1)
    assert generate_history(p, 200, 1) != generate_history(p, 200, 2)
    assert generate_history(p, 0, 1).cases == ()
    with pytest.raises(ValueError):
        generate_history(p, -1, 1)


def test_stripe_rendering_maps_back_to_the_same_records() -> None:
    p = truth_profile()
    objs = stripe_disputes(p, 400, 6)
    direct = generate_history(p, 400, 6, source="stripe")
    cases: list[ExternalCase] = []
    moves: list[LedgerMovement] = []
    for o in objs:
        assert o["metadata"] == SYNTHETIC_METADATA and o["id"].startswith("dp_synth")
        batch = ingest_dispute(o)
        cases.extend(batch.cases)
        moves.extend(batch.movements)
    assert tuple(cases) == direct.cases
    assert tuple(moves) == direct.movements


def test_stripe_rendering_needs_stripe_reasons() -> None:
    bad = DEFAULT_PROFILE.model_validate(
        {
            **DEFAULT_PROFILE.model_dump(),
            "reasons": {"not_a_stripe_reason": DEFAULT_PROFILE.reasons["general"].model_dump()}
            | {"general": {**DEFAULT_PROFILE.reasons["general"].model_dump(), "weight": 0.75}},
        }
    )
    with pytest.raises(ValueError, match="not Stripe"):
        stripe_disputes(bad, 5, 1)


def test_to_case_mix_is_opt_in_and_usable() -> None:
    h = generate_history(truth_profile(p_inquiry=0.3), 3_000, 8)
    fit = fit_profile(compute_aggregates(h.cases, h.movements))
    mix = to_case_mix(fit)
    assert math.isclose(mix.p_inquiry, fit.p_inquiry)
    assert set(mix.network_weights) == {Network.VISA, Network.MASTERCARD}
    assert len(generate_cases(seed=1, n=20, mix=mix)) == 20
