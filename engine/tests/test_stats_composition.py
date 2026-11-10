"""Composition of hard checks, scores and tests (docs/math/08, Theorem 8.1)."""

from __future__ import annotations

import itertools
import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.stats.composition import (
    certify_every_type,
    false_block_bound,
    iut_promote_bound,
    spending_levels,
)

levels = st.dictionaries(
    st.sampled_from(["mean", "type", "ledger", "process"]), st.floats(0.0, 1.0), min_size=1
)


@given(lv=levels, harms=st.lists(st.sampled_from(["mean", "type", "ledger", "other"])))
def test_iut_bound_is_the_smallest_covering_level(lv: dict[str, float], harms: list[str]) -> None:
    bound = iut_promote_bound(lv, harms)
    covered = [lv[h] for h in harms if h in lv]
    assert bound == (min(covered) if covered else 1.0)
    # adding a conjunct never raises the bound
    assert iut_promote_bound({**lv, "extra": 0.5}, [*harms, "extra"]) <= bound


@given(alpha=st.floats(0.0, 1.0), phis=st.lists(st.floats(0.0, 0.2), max_size=40))
def test_false_block_bound_matches_union(alpha: float, phis: list[float]) -> None:
    b = false_block_bound(alpha, phis)
    exact = 1.0 - math.prod(1.0 - p for p in phis)
    assert b == pytest.approx(min(1.0, alpha + exact))
    assert b <= min(1.0, alpha + sum(phis)) + 1e-12


def test_false_blocks_accumulate_with_n() -> None:
    phi = 19 / 10_000
    seq = [false_block_bound(0.05, [phi] * n) for n in (200, 500, 2000, 10_000)]
    assert seq == sorted(seq)
    assert seq[-1] == pytest.approx(1.0, abs=1e-6)
    assert false_block_bound(0.0, [1.0]) == 1.0


@given(alpha=st.floats(0.001, 0.5), k=st.integers(0, 500))
def test_spending_never_exceeds_alpha(alpha: float, k: int) -> None:
    for kind in ("basel", "geometric"):
        lv = spending_levels(alpha, k, kind)
        assert len(lv) == k
        assert sum(lv) <= alpha + 1e-12
        assert all(a > b for a, b in itertools.pairwise(lv))


def test_per_type_certificate_is_valid_without_bonferroni() -> None:
    """One type at its tolerance, the rest clearly better: P(all certified) <= alpha."""
    reps, alpha = 300, 0.1
    tol = {"a": 0.0, "b": 0.0, "c": 0.0}
    rng = random.Random(8)
    hits = 0
    for _ in range(reps):
        stream = []
        for _ in range(600):
            k = rng.choice("abc")
            mu = 0.0 if k == "a" else -0.3
            stream.append((k, mu + rng.uniform(-0.5, 0.5)))
        hits += certify_every_type(stream, tol, alpha).all_certified
    assert hits / reps <= alpha + 3 * math.sqrt(alpha * (1 - alpha) / reps)


def test_per_type_certificate_blocks_a_rare_harmful_type() -> None:
    rng = random.Random(9)
    tol = {"common": 0.0, "rare": 0.0}
    stream = []
    for _ in range(4000):
        k = "rare" if rng.random() < 0.02 else "common"
        mu = 0.35 if k == "rare" else -0.05
        stream.append((k, mu + rng.uniform(-0.5, 0.5)))
    res = certify_every_type(stream, tol, 0.05)
    assert "common" in res.certified and "rare" not in res.certified
    assert not res.all_certified


def test_validation() -> None:
    with pytest.raises(ValueError):
        iut_promote_bound({"x": 1.5}, ["x"])
    with pytest.raises(ValueError):
        false_block_bound(1.5, [])
    with pytest.raises(ValueError):
        false_block_bound(0.1, [1.5])
    with pytest.raises(ValueError):
        spending_levels(0.0, 3)
    with pytest.raises(ValueError):
        spending_levels(0.1, -1)
    with pytest.raises(ValueError):
        spending_levels(0.1, 2, "harmonic")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        certify_every_type([], {}, 0.1)
    with pytest.raises(ValueError):
        certify_every_type([("z", 0.0)], {"a": 0.0}, 0.1)
    with pytest.raises(ValueError):
        certify_every_type([], {"a": 0.0}, 1.0)
