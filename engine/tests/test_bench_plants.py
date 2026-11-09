"""Planted regressions, controls and the detector used by experiment E1."""

from __future__ import annotations

from functools import cache

import pytest

from scruple.bench.detect import detect, folds
from scruple.bench.plants import CONTROLS, REGRESSIONS, Planted, picked
from scruple.bench.runner import Agent, BlockResult, run_agent
from scruple.bench.spec import BenchCase, bench_cases
from scruple.sim.splits import SeedSpace

SALT = "plants-test-salt-0123456789"


@cache
def cases() -> tuple[BenchCase, ...]:
    return tuple(bench_cases(SeedSpace(SALT), 3, range(60)))


@cache
def run(p: Planted) -> list[BlockResult]:
    return run_agent(Agent(p.name, p), cases(), block=30)


def test_picked_is_deterministic_and_near_its_rate() -> None:
    ids = [f"ep-{i:06d}" for i in range(4000)]
    share = sum(picked(i, 0.05) for i in ids) / len(ids)
    assert 0.04 < share < 0.06
    assert [picked(i, 0.05) for i in ids] == [picked(i, 0.05) for i in ids]
    assert not any(picked(i, 0.0) for i in ids) and all(picked(i, 1.0) for i in ids)
    assert [picked(i, 0.5, "a") for i in ids] != [picked(i, 0.5, "b") for i in ids]


def test_names_are_unique() -> None:
    names = [p.name for p in (*REGRESSIONS, *CONTROLS)]
    assert len(set(names)) == len(names) == 11
    assert Planted("none").name == "none"


def test_unmodified_is_never_flagged() -> None:
    base = run(CONTROLS[0])
    d = detect(base, base)
    assert not d.flagged and d.changed == 0 and d.gate_outcome != "block"


@pytest.mark.parametrize(
    ("idx", "check"),
    [(5, "PROC-01"), (6, "SR-01")],
    ids=["R6", "R7"],
)
def test_hard_check_regressions_are_flagged(idx: int, check: str) -> None:
    base, cand = run(CONTROLS[0]), run(REGRESSIONS[idx])
    d = detect(base, cand)
    assert d.flagged and d.verifier and d.new_failures_by_check.get(check, 0) > 0
    assert d.exercised and d.new_hard_failures <= d.changed <= d.n_cases
    per = [detect(b, c) for b, c in zip(folds(base), folds(cand), strict=True)]
    assert len(per) == 2 and all(f.flagged for f in per if f.new_hard_failures)


def test_late_any_misses_deadlines() -> None:
    p = Planted("late_any", rate=1.0)
    d = detect(run(CONTROLS[0]), run(p))
    assert d.new_failures_by_check.get("DL-01", 0) > 0


def test_controls_break_no_hard_check() -> None:
    base = run(CONTROLS[0])
    for c in CONTROLS[1:]:
        d = detect(base, run(c))
        assert d.new_hard_failures == 0 and not d.batch, c.name
        assert d.exercised, c.name


def test_small_amount_regressions_change_cost() -> None:
    base = run(CONTROLS[0])
    for p in (
        Planted("concede_small", small_minor=10**9),
        Planted("over_refund_small", small_minor=10**9),
    ):
        d = detect(base, run(p))
        assert d.changed > 0 and d.mean_cost_diff > 0, p.kind


def test_wrong_evidence_on_every_code() -> None:
    base = run(CONTROLS[0])
    hits = 0
    for code in ("13.1", "4855", "13.3", "4853"):
        d = detect(base, run(Planted("wrong_evidence_code", reason_code=code)))
        hits += d.changed
    assert hits > 0


def test_detect_refuses_unpaired_runs() -> None:
    base = run(CONTROLS[0])
    with pytest.raises(ValueError, match="blocks"):
        detect(base, base[:1])
    other = run_agent(Agent("x", CONTROLS[0]), bench_cases(SeedSpace(SALT), 3, range(29)), block=30)
    with pytest.raises(ValueError, match="cases"):
        detect(base[:1], other)
