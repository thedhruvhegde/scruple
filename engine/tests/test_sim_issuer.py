from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import CardDispute, CaseMix, DisputeEngine, Represent, generate_cases
from scruple.sim.evidence import document_problems, make_document
from scruple.sim.issuer import DECISION_STAGES, IssuerParams, SeededIssuer
from scruple.sim.models import Stage
from scruple.sim.reason_codes import ReasonCodeSpec, load_catalogue

CODES = load_catalogue().codes
ISSUER = SeededIssuer()


def with_code(spec: ReasonCodeSpec) -> CardDispute:
    base = next(c for c in generate_cases(1, 50) if c.network is spec.network)
    docs = tuple(
        make_document(
            base.case_id, k, base.processed_on, {"amount_minor": str(base.amount_minor)}, i
        )
        for i, k in enumerate(spec.all_evidence)
    )
    return base.model_copy(update={"reason_code": spec.code, "documents": docs})


@pytest.mark.parametrize("spec", CODES, ids=[c.key for c in CODES])
@pytest.mark.parametrize("stage", DECISION_STAGES)
def test_full_evidence_beats_no_evidence(spec: ReasonCodeSpec, stage: Stage) -> None:
    case = with_code(spec)
    none = ISSUER.odds(case, stage, ())
    full = ISSUER.odds(case, stage, case.documents)
    assert none == IssuerParams().base[stage]
    assert full > none
    assert 0 <= none < full <= IssuerParams().cap


@pytest.mark.parametrize("stage", DECISION_STAGES)
def test_later_stages_reward_evidence_more(stage: Stage) -> None:
    p = IssuerParams()
    assert 0 < p.weight[stage] <= 1 and 0 <= p.base[stage] < 1


def test_decisions_are_deterministic_and_seeded() -> None:
    case = with_code(CODES[0])
    a = SeededIssuer(IssuerParams(seed=1)).decide(case, Stage.REPRESENTMENT, case.documents)
    b = SeededIssuer(IssuerParams(seed=1)).decide(case, Stage.REPRESENTMENT, case.documents)
    c = SeededIssuer(IssuerParams(seed=2)).decide(case, Stage.REPRESENTMENT, case.documents)
    assert a == b
    assert a.draw != c.draw


def test_issuer_refuses_non_decision_stages() -> None:
    case = with_code(CODES[0])
    with pytest.raises(ValueError, match="does not decide"):
        ISSUER.decide(case, Stage.CHARGEBACK, ())


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 10_000), data=st.data())
def test_more_genuine_evidence_never_hurts(seed: int, data: st.DataObject) -> None:
    case = data.draw(st.sampled_from(generate_cases(seed, 20)))
    docs = case.documents
    k = data.draw(st.integers(0, len(docs)))
    stage = data.draw(st.sampled_from(DECISION_STAGES))
    issuer = SeededIssuer(IssuerParams(seed=seed))
    fewer = issuer.decide(case, stage, docs[:k])
    more = issuer.decide(case, stage, docs)
    assert more.odds >= fewer.odds
    assert more.draw == fewer.draw
    if fewer.merchant_prevails:
        assert more.merchant_prevails


def test_acceptance_rate_tracks_the_odds() -> None:
    case = with_code(CODES[0])
    stage = Stage.REPRESENTMENT
    evidence = case.documents[:1]
    odds = ISSUER.odds(case, stage, evidence)
    wins = sum(
        SeededIssuer(IssuerParams(seed=s)).merchant_prevails(case, stage, evidence)
        for s in range(3_000)
    )
    assert abs(wins / 3_000 - odds) < 0.03


def test_detected_forgery_loses_and_undetected_counts() -> None:
    cases = generate_cases(3, 400, CaseMix(p_forged_evidence=1.0))
    forged_cases = [c for c in cases if c.truth.forged]
    assert forged_cases
    caught = 0
    for c in forged_cases:
        d = ISSUER.decide(c, Stage.ARBITRATION, c.documents)
        if d.caught:
            caught += 1
            assert not d.merchant_prevails and d.odds == 0
            assert set(d.caught) <= set(c.truth.forged)
    assert caught / len(forged_cases) > 0.8  # p_detect at arbitration is 0.95


def test_genuine_documents_are_never_caught() -> None:
    for c in generate_cases(4, 200):
        for d in c.documents:
            assert document_problems(d, c.amount_minor, c.currency, c.processed_on) == ()
        for stage in DECISION_STAGES:
            assert ISSUER.decide(c, stage, c.documents).caught == ()


def test_merchants_in_the_right_win_more_often_with_the_default_issuer() -> None:
    cases = sorted(generate_cases(12, 600), key=lambda c: c.opened_on)
    stage = Stage.REPRESENTMENT
    right = [
        ISSUER.merchant_prevails(c, stage, c.documents) for c in cases if not c.truth.claim_valid
    ]
    wrong = [ISSUER.merchant_prevails(c, stage, c.documents) for c in cases if c.truth.claim_valid]
    assert sum(right) / len(right) > sum(wrong) / len(wrong) + 0.2


def test_engine_uses_the_seeded_issuer_by_default() -> None:
    c = next(x for x in generate_cases(2, 50) if x.opening_stage is Stage.CHARGEBACK)
    clock = ManualClock()
    clock.set_date(c.opened_on)
    e = DisputeEngine([c], clock)
    assert isinstance(e.issuer, SeededIssuer)
    assert e.act(c.case_id, Represent(evidence_ids=c.evidence_ids)).ok
