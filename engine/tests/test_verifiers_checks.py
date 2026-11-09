"""061: weighted checks with evidence, the scorecard and its hard gate."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.sim.engine import Simulator
from scruple.sim.generator import generate_cases
from scruple.sim.library import baseline_policy
from scruple.verifiers.checks import (
    OUTCOME_CORRECT,
    RECORD_INTEGRITY,
    CheckResult,
    FnBatchCheck,
    FnCheck,
    Scorecard,
    from_report,
    naive_weighted,
    run_checks,
)
from scruple.verifiers.dispute import DISPUTE_CHECKS, score_batch, score_record
from scruple.verifiers.exploits.tamper import drop_losses_from_trace, rewrite_outcome
from scruple.verifiers.ledger import verify_ledger

SEEDS = range(16)


def _result(cid: str, weight: float, hard: bool, passed: bool, score: float) -> CheckResult:
    return CheckResult(id=cid, name=cid, weight=weight, hard=hard, passed=passed, score=score)


@given(
    st.lists(st.tuples(st.floats(0.1, 10), st.booleans(), st.floats(0, 1)), min_size=1, max_size=12)
)
def test_quality_is_the_weighted_mean_and_hard_failures_zero_reward(
    rows: list[tuple[float, bool, float]],
) -> None:
    checks = tuple(
        _result(
            f"c{i}",
            w,
            hard,
            (s >= 0.5) if hard else True,
            (1.0 if s >= 0.5 else 0.0) if hard else s,
        )
        for i, (w, hard, s) in enumerate(rows)
    )
    card = Scorecard(episode_id="ep", checks=checks)
    q = sum(c.weight * c.score for c in checks) / sum(c.weight for c in checks)
    assert card.quality == pytest.approx(q)
    hard_fail = any(c.hard and not c.passed for c in checks)
    assert card.gate is not hard_fail
    assert card.reward == (0.0 if hard_fail else pytest.approx(q))
    assert set(card.hard_failures) == {c.id for c in checks if c.hard and not c.passed}
    assert 0.0 <= card.reward <= 1.0


def test_volume_cannot_buy_back_a_violation() -> None:
    soft = tuple(_result(f"s{i}", 5.0, False, True, 1.0) for i in range(10))
    card = Scorecard(episode_id="ep", checks=(*soft, _result("h", 1.0, True, False, 0.0)))
    assert naive_weighted(card) > 0.95
    assert card.reward == 0.0 and not card.gate


def test_fn_check_clamps_and_forces_hard_scores() -> None:
    hard = FnCheck("H", "h", lambda r: (False, 0.9, (), "x"), hard=True)
    soft = FnCheck("S", "s", lambda r: (True, 7.0, (), "x"))
    rec = run_policy(evidence_policy, 1).record()
    assert hard.run(rec).score == 0.0
    assert soft.run(rec).score == 1.0
    batch = FnBatchCheck("B", "b", lambda rs: (len(rs) == 2, 0.5, (), "n"), hard=True)
    assert batch.run_batch([rec, rec]).score == 1.0


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("policy", [evidence_policy, always_accept])
def test_honest_records_pass_integrity_and_emit_evidence(seed: int, policy: object) -> None:
    rec = run_policy(policy, seed).record()  # type: ignore[arg-type]
    card = score_record(rec)
    integrity = card.get("INV-05")
    assert integrity.passed and integrity.hard
    correct = card.get("OUT-01")
    assert correct.evidence  # points at the case truth and the journal
    assert correct.passed is (rec.outcome is not None and rec.outcome.correct)
    assert [c.id for c in card.checks] == [c.id for c in DISPUTE_CHECKS]


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("tamper", [rewrite_outcome, drop_losses_from_trace])
def test_rewritten_records_fail_integrity(seed: int, tamper: object) -> None:
    rec = run_policy(always_accept, seed).record()
    bad = tamper(rec)  # type: ignore[operator]
    card = score_record(bad)
    if bad is rec:
        assert card.gate
        return
    assert not card.gate
    assert card.get("OUT-01").passed  # the naive part is fooled
    assert card.reward == 0.0
    assert card.get("INV-05").explanation.startswith("inconsistent")


def test_episode_score_carries_the_scorecard() -> None:
    ep = run_policy(evidence_policy, 3)
    assert ep.score is not None and ep.score.scorecard is not None
    dumped = ep.score.model_dump(mode="json")
    assert set(dumped["scorecard"]) >= {"gate", "quality", "reward", "checks"}
    assert ep.score.gate is ep.score.scorecard.gate
    assert ep.score.reward == ep.score.scorecard.reward


def test_batch_scorecard_aggregates() -> None:
    recs = [run_policy(evidence_policy, s).record() for s in range(6)]
    batch = score_batch(recs)
    assert len(batch.episodes) == 6
    assert batch.episode_gate_rate == 1.0
    assert batch.mean_reward == pytest.approx(sum(e.reward for e in batch.episodes) / 6)
    assert run_checks(recs[0], (OUTCOME_CORRECT, RECORD_INTEGRITY)).episode_id == recs[0].episode_id


def test_legacy_reports_become_checks() -> None:
    cases = generate_cases(1, 200)
    sim = Simulator(cases)
    sim.run(baseline_policy())
    report = verify_ledger(cases, sim.result())
    for c in cases[:20]:
        res = from_report(report, c.case_id, weight=2.0)
        assert res.id == "legacy.ledger_correctness"
        assert res.passed is not any(f.case_id in (c.case_id, "*") for f in report.findings)
        assert res.score == (1.0 if res.passed else 0.0)
