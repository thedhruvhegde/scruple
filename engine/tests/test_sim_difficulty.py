"""Difficulty knob (032): one number moves ambiguity and rarity, monotonically."""

from __future__ import annotations

import itertools
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.hashing import canonical_json
from scruple.sim.difficulty import (
    DifficultyMetrics,
    coverage,
    difficulty_mix,
    family_shares,
    family_weights,
    informative,
    measure,
    required_holding_gap,
)
from scruple.sim.disputes import (
    DEFAULT_MIX,
    FULL_MIX,
    P_REQUIRED_HELD,
    P_SUPPORTING_HELD,
    CaseMix,
    generate_cases,
)
from scruple.sim.families import FAMILY_WEIGHTS, CaseFamily

GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


def _json(mix: CaseMix, seed: int = 9, n: int = 50) -> str:
    return canonical_json([c.model_dump(mode="json") for c in generate_cases(seed, n, mix)])


def test_half_difficulty_is_exactly_the_full_mix() -> None:
    assert difficulty_mix(0.5) == FULL_MIX
    assert _json(difficulty_mix(0.5)) == _json(FULL_MIX)


def test_new_mix_fields_default_to_the_old_constants() -> None:
    m = CaseMix()
    assert m.p_required_held_invalid == P_REQUIRED_HELD[False]
    assert m.p_required_held_valid == P_REQUIRED_HELD[True]
    assert m.p_supporting_held_invalid == P_SUPPORTING_HELD[False]
    assert m.p_supporting_held_valid == P_SUPPORTING_HELD[True]
    assert m == DEFAULT_MIX


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_out_of_range_difficulty_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="difficulty"):
        difficulty_mix(bad)


@given(unit)
def test_every_difficulty_gives_a_valid_mix(d: float) -> None:
    m = difficulty_mix(d)
    CaseMix.model_validate(m.model_dump())  # all probabilities in range
    assert m.family_weights is not None and all(w > 0 for w in m.family_weights.values())
    assert required_holding_gap(d) > 0  # the classes converge but never cross


@given(unit, unit)
def test_expected_ambiguity_is_monotone(a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    ml, mh = difficulty_mix(lo), difficulty_mix(hi)
    assert required_holding_gap(hi) <= required_holding_gap(lo)
    assert mh.p_required_held_valid >= ml.p_required_held_valid
    assert mh.p_required_held_invalid <= ml.p_required_held_invalid
    assert mh.p_supporting_held_valid >= ml.p_supporting_held_valid
    assert mh.p_supporting_held_invalid <= ml.p_supporting_held_invalid
    assert mh.p_missing_evidence >= ml.p_missing_evidence
    assert mh.p_forged_evidence >= ml.p_forged_evidence


def _entropy(s: dict[CaseFamily, float]) -> float:
    return -sum(p * math.log(p) for p in s.values() if p > 0)


@given(unit, unit)
def test_expected_rarity_is_monotone(a: float, b: float) -> None:
    lo, hi = sorted((a, b))
    sl, sh = family_shares(lo), family_shares(hi)
    assert min(sh.values()) <= min(sl.values()) + 1e-12
    assert math.isclose(sum(sh.values()), 1.0)
    assert _entropy(sh) <= _entropy(sl) + 1e-12


def test_family_weight_endpoints() -> None:
    assert set(family_weights(0.0).values()) == {1.0}
    assert family_weights(0.5) == FAMILY_WEIGHTS
    for f, w in FAMILY_WEIGHTS.items():
        assert math.isclose(family_weights(1.0)[f], w * w)


@pytest.fixture(scope="module")
def grid_metrics() -> dict[int, list[DifficultyMetrics]]:
    return {s: [measure(generate_cases(s, 2_500, difficulty_mix(d))) for d in GRID] for s in (1, 2)}


@pytest.mark.parametrize("seed", [1, 2])
@pytest.mark.parametrize("metric", ["coverage_overlap", "bayes_error"])
def test_measured_ambiguity_rises_with_difficulty(
    grid_metrics: dict[int, list[DifficultyMetrics]], seed: int, metric: str
) -> None:
    vals = [getattr(m, metric) for m in grid_metrics[seed]]
    assert all(a < b for a, b in itertools.pairwise(vals)), vals
    assert vals[-1] - vals[0] > 0.3  # a real effect, not noise


@pytest.mark.parametrize("seed", [1, 2])
def test_measured_rarity_rises_with_difficulty(
    grid_metrics: dict[int, list[DifficultyMetrics]], seed: int
) -> None:
    ms = grid_metrics[seed]
    rare = [m.rarest_share for m in ms]
    ent = [m.family_entropy for m in ms]
    assert all(a > b for a, b in itertools.pairwise(rare)), rare
    assert all(a > b for a, b in itertools.pairwise(ent)), ent
    assert ms[0].rare_families == 0 and ms[-1].rare_families >= 2


def test_easiest_setting_lets_evidence_decide() -> None:
    m = measure(generate_cases(4, 2_000, difficulty_mix(0.0)))
    assert m.bayes_error < 0.05  # only split shipments and CE 3.0 histories blur it


def test_measure_counts_and_bounds() -> None:
    cases = generate_cases(5, 400, difficulty_mix(0.7))
    m = measure(cases)
    assert m.n == 400 and sum(m.family_counts.values()) == 400
    assert m.n_informative == sum(1 for c in cases if informative(c))
    assert 0 <= m.coverage_overlap <= 1 and 0 <= m.bayes_error <= 0.5
    assert all(0.0 <= coverage(c) <= 1.0 for c in cases)


def test_measure_refuses_empty_input() -> None:
    with pytest.raises(ValueError, match="no cases"):
        measure([])


def test_measure_with_one_class_only() -> None:
    mix = difficulty_mix(0.3).model_copy(update={"family_weights": {CaseFamily.TRUE_FRAUD: 1.0}})
    m = measure(generate_cases(1, 100, mix))
    assert m.coverage_overlap == 0.0 and m.bayes_error == 0.0
    assert m.rarest_share == 0.0 and m.rare_families == len(CaseFamily) - 1
