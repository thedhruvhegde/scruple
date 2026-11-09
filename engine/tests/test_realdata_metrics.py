"""Metric implementations: hand-computed values, sklearn agreement, bootstrap coverage."""

from __future__ import annotations

import importlib
import importlib.util
import math
import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.realdata import metrics as M  # noqa: N812

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None


def _sk() -> Any:
    return importlib.import_module("sklearn.metrics")


# --- hand-computed ---------------------------------------------------------------

Y = ["a", "a", "a", "b", "b", "c"]
P = ["a", "a", "b", "b", "c", "c"]


def test_accuracy_hand() -> None:
    assert M.accuracy(Y, P) == pytest.approx(4 / 6)
    assert M.accuracy([], []) is None


def test_per_class_and_macro_f1_hand() -> None:
    # a: tp2 gold3 pred2 -> f1 = 4/5; b: tp1 gold2 pred2 -> 1/2; c: tp1 gold1 pred2 -> 2/3
    f = M.per_class_f1(Y, P)
    assert f == pytest.approx({"a": 0.8, "b": 0.5, "c": 2 / 3})
    assert M.macro_f1(Y, P) == pytest.approx((0.8 + 0.5 + 2 / 3) / 3)
    assert M.per_class_recall(Y, P) == pytest.approx({"a": 2 / 3, "b": 0.5, "c": 1.0})
    assert M.balanced_accuracy(Y, P) == pytest.approx((2 / 3 + 0.5 + 1) / 3)


def test_invalid_predictions_count_against_recall_only() -> None:
    y = ["a", "b"]
    p = ["a", "<invalid>"]
    assert M.per_class_f1(y, p) == {"a": 1.0, "b": 0.0}
    assert M.macro_f1(y, p) == 0.5


def test_confusion_hand() -> None:
    assert M.confusion(Y, P) == {"a": {"a": 2, "b": 1}, "b": {"b": 1, "c": 1}, "c": {"c": 1}}


def test_length_mismatch() -> None:
    with pytest.raises(ValueError):
        M.accuracy(["a"], [])


def test_auroc_hand() -> None:
    # pairs (pos, neg): (0.8,0.1)+ (0.8,0.4)+ (0.35,0.1)+ (0.35,0.4)- -> 3/4
    assert M.auroc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == pytest.approx(0.75)
    assert M.auroc([0, 1], [0.5, 0.5]) == pytest.approx(0.5)  # tie counts half
    assert M.auroc([1, 1], [0.2, 0.3]) is None
    assert M.auroc([0, 1, 1], [0.9, 0.1, 0.2]) == 0.0


def test_brier_hand() -> None:
    assert M.brier([1, 0], [0.8, 0.4]) == pytest.approx((0.04 + 0.16) / 2)
    assert M.brier([], []) is None


def test_ece_hand() -> None:
    # bins of width .5: [0.2, 0.3] (y 0, 1) -> |0.5 - 0.25| ; [0.9, 1.0] (y 1, 1) -> |1 - .95|
    got = M.ece([0, 1, 1, 1], [0.2, 0.3, 0.9, 1.0], bins=2)
    assert got == pytest.approx(0.5 * 0.25 + 0.5 * 0.05)
    assert M.ece([1], [1.0]) == 0.0
    with pytest.raises(ValueError):
        M.ece([1], [0.5], bins=0)


def test_reliability_table() -> None:
    rows = M.reliability([0, 1, 1], [0.05, 0.95, 1.0])
    assert [r["n"] for r in rows] == [1.0, 2.0]
    assert rows[1]["rate"] == 1.0
    assert rows[1]["mean_p"] == pytest.approx(0.975)


def test_percentile_matches_numpy_linear() -> None:
    v = [1.0, 2.0, 3.0, 4.0]
    assert M.percentile(v, 0.0) == 1.0
    assert M.percentile(v, 1.0) == 4.0
    assert M.percentile(v, 0.5) == 2.5
    assert M.percentile(v, 0.25) == pytest.approx(1.75)
    with pytest.raises(ValueError):
        M.percentile([], 0.5)
    with pytest.raises(ValueError):
        M.percentile(v, 1.5)


def test_bootstrap_is_seeded_and_brackets_point() -> None:
    y = [1, 0] * 50
    p = [1, 0] * 40 + [0, 1] * 10
    a = M.labels_ci(M.accuracy, y, p, n_boot=200, seed=3)
    b = M.labels_ci(M.accuracy, y, p, n_boot=200, seed=3)
    assert a == b
    assert a.value == 0.8
    assert a.lo is not None and a.hi is not None
    assert a.lo <= 0.8 <= a.hi
    assert a.n_boot == 200
    assert M.labels_ci(M.accuracy, y, p, n_boot=200, seed=4) != a


def test_bootstrap_edge_cases() -> None:
    assert M.labels_ci(M.accuracy, [], [], n_boot=10).value is None
    e = M.labels_ci(M.accuracy, ["a"], ["a"], n_boot=0)
    assert e.value == 1.0 and e.lo is None
    # AUROC undefined on resamples with one class: skipped and counted
    e2 = M.scores_ci(M.auroc, [0, 1], [0.1, 0.9], n_boot=200, seed=1)
    assert e2.n_boot_undefined > 0
    assert e2.n_boot + e2.n_boot_undefined == 200
    e3 = M.scores_ci(M.auroc, [1, 1], [0.1, 0.9], n_boot=10)
    assert e3.value is None and e3.n_boot == 0


# --- properties --------------------------------------------------------------------

labels = st.lists(st.sampled_from("abcd"), min_size=1, max_size=40)


@given(labels, st.randoms(use_true_random=False))
def test_metric_ranges(y: list[str], rnd: random.Random) -> None:
    p = [rnd.choice("abcde") for _ in y]
    for fn in (M.accuracy, M.macro_f1, M.balanced_accuracy):
        v = fn(y, p)
        assert v is not None and 0.0 <= v <= 1.0
    assert M.accuracy(y, y) == M.macro_f1(y, y) == M.balanced_accuracy(y, y) == 1.0


scores = st.lists(
    st.tuples(st.integers(0, 1), st.integers(0, 100).map(lambda k: k / 100)),
    min_size=2,
    max_size=60,
)


@given(scores)
def test_auroc_flip_symmetry_and_ranges(pairs: list[tuple[int, float]]) -> None:
    y = [a for a, _ in pairs]
    s = [b for _, b in pairs]
    a = M.auroc(y, s)
    if len(set(y)) < 2:
        assert a is None
        return
    assert a is not None and 0.0 <= a <= 1.0
    flipped = M.auroc(y, [1 - v for v in s])
    assert flipped is not None
    assert a + flipped == pytest.approx(1.0)
    b = M.brier(y, s)
    e = M.ece(y, s)
    assert b is not None and 0.0 <= b <= 1.0
    assert e is not None and 0.0 <= e <= 1.0


# --- sklearn agreement ---------------------------------------------------------------


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
@settings(max_examples=60, deadline=None)
@given(labels, st.randoms(use_true_random=False))
def test_classification_matches_sklearn(y: list[str], rnd: random.Random) -> None:
    sk = _sk()
    p = [rnd.choice("abcde") for _ in y]
    gold = sorted(set(y))
    assert M.accuracy(y, p) == pytest.approx(sk.accuracy_score(y, p))
    assert M.macro_f1(y, p) == pytest.approx(
        sk.f1_score(y, p, labels=gold, average="macro", zero_division=0)
    )
    rec = sk.recall_score(y, p, labels=gold, average=None, zero_division=0)
    assert [M.per_class_recall(y, p)[c] for c in gold] == pytest.approx(list(rec))
    assert M.balanced_accuracy(y, p) == pytest.approx(
        sum(rec) / len(rec)  # sklearn's balanced_accuracy_score warns on unseen preds
    )


@pytest.mark.skipif(not HAS_SKLEARN, reason="scikit-learn not installed")
@settings(max_examples=60, deadline=None)
@given(scores)
def test_probability_metrics_match_sklearn(pairs: list[tuple[int, float]]) -> None:
    sk = _sk()
    y = [a for a, _ in pairs]
    s = [b for _, b in pairs]
    assert M.brier(y, s) == pytest.approx(sk.brier_score_loss(y, s, pos_label=1))
    if len(set(y)) == 2:
        assert M.auroc(y, s) == pytest.approx(sk.roc_auc_score(y, s))


# --- CI coverage by simulation ---------------------------------------------------------


def _coverage(trials: int, n: int, n_boot: int, p_true: float, seed: int) -> float:
    rng = random.Random(seed)
    hits = 0
    for t in range(trials):
        y = [1] * n
        pred = [1 if rng.random() < p_true else 0 for _ in range(n)]
        est = M.labels_ci(M.accuracy, y, pred, n_boot=n_boot, seed=t)
        assert est.lo is not None and est.hi is not None
        hits += est.lo <= p_true <= est.hi
    return hits / trials


def test_bootstrap_ci_coverage_near_nominal() -> None:
    cov = _coverage(trials=150, n=150, n_boot=200, p_true=0.7, seed=11)
    # nominal 0.95; the percentile bootstrap under-covers slightly at small n
    assert 0.88 <= cov <= 0.995, cov


@pytest.mark.slow
def test_bootstrap_ci_coverage_auroc() -> None:
    rng = random.Random(5)
    hits = 0
    trials = 150
    # scores ~ N(1, 1) for positives and N(0, 1) for negatives: true AUROC = Phi(1/sqrt 2)
    true_auc = 0.5 * (1 + math.erf(0.5))
    for t in range(trials):
        y = [1] * 80 + [0] * 120
        s = [rng.gauss(1.0 if v else 0.0, 1.0) for v in y]
        lo_hi = M.scores_ci(M.auroc, y, s, n_boot=300, seed=t)
        assert lo_hi.lo is not None and lo_hi.hi is not None
        hits += lo_hi.lo <= true_auc <= lo_hi.hi
    assert 0.88 <= hits / trials <= 0.995
