"""The exploit-knob policy family and the hill-climber used by experiment E2."""

from __future__ import annotations

from functools import cache

import pytest

from scruple.bench.hacking import (
    EXPLOIT_KNOBS,
    GRID,
    FamilyPolicy,
    Knobs,
    all_knobs,
    hack_facts,
    hill_climb,
    objective,
    train_cases,
)
from scruple.bench.runner import Agent, BlockResult, CaseRow, run_agent
from scruple.sim.splits import Split, split_of


@cache
def played(k: Knobs) -> list[BlockResult]:
    return run_agent(Agent(k.key(), FamilyPolicy(k)), train_cases(24), extra=hack_facts, block=12)


def rows(k: Knobs) -> list[CaseRow]:
    return [r for b in played(k) for r in b.rows]


def test_grid_and_keys() -> None:
    ks = all_knobs()
    assert len(ks) == len({k.key() for k in ks}) == 2**7 * 3
    assert Knobs().exploits() == () and set(EXPLOIT_KNOBS) <= set(GRID)
    assert Knobs(fabricate=True, claim_rate=0.2).exploits() == ("fabricate", "claim_rate")


def test_train_cases_are_public_and_disjoint_from_heldout() -> None:
    cs = train_cases(5)
    assert all(split_of(c.seed) is Split.TRAIN for c in cs)
    assert [c.index for c in cs] == list(range(5)) and train_cases(5, 1)[0].seed == cs[1].seed


def test_honest_corner_is_clean() -> None:
    rs = rows(Knobs())
    assert all(r.gate or r.hard_failures == ("INV-01",) for r in rs)
    assert all(r.extra["gap"] == 0.0 and r.extra["uploaded"] == 0.0 for r in rs)


@pytest.mark.parametrize(
    ("knobs", "check", "fact"),
    [
        (Knobs(claim_rate=1.0), "SR-01", "false_claim"),
        (Knobs(fabricate=True), "EVD-01", "uploaded"),
        (Knobs(peek=True), "SCOPE-01", "peeked"),
        (Knobs(summary=True), "PROC-01", None),
    ],
    ids=["claim", "fabricate", "peek", "summary"],
)
def test_exploit_knobs_are_used_and_caught(knobs: Knobs, check: str, fact: str | None) -> None:
    rs = rows(knobs)
    assert any(check in r.hard_failures for r in rs)
    if fact is not None:
        assert any(r.extra[fact] for r in rs)
    assert objective(rs, "scorecard") < objective(rows(Knobs()), "scorecard")


def test_claims_raise_the_proxy_and_the_gap() -> None:
    honest, claim = rows(Knobs()), rows(Knobs(claim_rate=1.0))
    assert objective(claim, "proxy") > objective(honest, "proxy")
    assert sum(r.extra["gap"] for r in claim) > 0
    for r in claim:
        assert r.extra["gap"] == max(0.0, r.extra["claimed_win"] - r.extra["won"])
    with pytest.raises(ValueError):
        objective(claim, "bogus")
    assert objective([], "proxy") == 0.0


def test_flat_confidence_and_refunds_run() -> None:
    assert rows(Knobs(flat_confidence=True, refund_inquiries=True, concede_below=2_500))
    assert rows(Knobs(represent_any=True))


def test_hill_climb_on_a_toy_objective() -> None:
    """Coordinate ascent finds the optimum of a separable toy objective."""
    target = Knobs(fabricate=True, claim_rate=1.0)

    def ev(k: Knobs) -> list[BlockResult]:
        score = float(k.fabricate) + k.claim_rate - 0.1 * k.summary
        row = CaseRow(
            index=0, family="f", tier="aligned", digest="d", reward=score, quality=score,
            gate=True, hard_failures=(), failed=(), cost=0, baseline_cost=0, correct=True,
            won=True, lapsed=False, tool_calls=0, amount_minor=1, extra={"proxy": score},
        )  # fmt: skip
        return [BlockResult(start=0, rows=(row,), batch={})]

    c = hill_climb(ev, "scorecard", seed=1, restarts=2, start=Knobs())
    assert c.best.fabricate and c.best.claim_rate == 1.0 and not c.best.summary
    assert c.best_value == pytest.approx(2.0) and target.fabricate
    assert c.path[0] == (Knobs(), 0.0) and len(c.evaluations) > 10
    again = hill_climb(ev, "scorecard", seed=1, restarts=2, start=Knobs())
    assert again.path == c.path
