"""End-to-end use of the public dispute API, as another agent would use it."""

from __future__ import annotations

from scruple.hashing import content_hash
from scruple.sim.deadlines import ManualClock
from scruple.sim.disputes import (
    ACTION_KINDS,
    FULL_MIX,
    Accept,
    AgentAction,
    DisputeEngine,
    Escalate,
    Refund,
    Represent,
    RequestInfo,
    generate_cases,
)
from scruple.sim.families import CaseFamily
from scruple.sim.models import Stage


def careful_agent(e: DisputeEngine, cid: str) -> AgentAction | None:
    obs = e.observe(cid)
    acts = obs.allowed_actions
    if not acts:
        return None
    ids = tuple(d.doc_id for d in obs.evidence)
    if "escalate" in acts:
        return Escalate() if len(ids) >= 4 else Accept()
    if "represent" in acts:
        return (
            Represent(evidence_ids=ids)
            if len(ids) >= 3
            else (
                Accept()
                if "accept" in acts
                else Refund(amount_minor=int(str(obs.case["amount_minor"])))
            )
        )
    return None


def run(seed: int, n: int = 150) -> str:
    cases = sorted(generate_cases(seed, n, FULL_MIX), key=lambda c: c.opened_on)
    clock = ManualClock()
    clock.set_date(cases[0].opened_on)
    e = DisputeEngine(cases, clock)
    for _ in range(400):
        for cid in e.open_cases():
            a = careful_agent(e, cid)
            if a is not None:
                e.act(cid, a)
        if not e.open_cases():
            break
        clock.advance(days=1)
    assert not e.open_cases()
    assert all(v == 0 for v in e.ledger.currency_totals().values())
    assert set(e.ledger.over_credited()) <= {v.txn_id for v in e.ledger.violations}
    outcomes = [e.outcome(c.case_id) for c in cases]
    assert all(o is not None for o in outcomes)
    return content_hash(
        {
            "outcomes": [o.model_dump(mode="json") for o in outcomes if o is not None],
            "journal": [x.model_dump(mode="json") for x in e.ledger.entries],
            "incidents": [i.model_dump(mode="json") for i in e.incidents],
        }
    )


def test_full_run_is_byte_identical_across_runs() -> None:
    assert run(21) == run(21)
    assert run(21) != run(22)


def test_full_mix_exercises_every_feature() -> None:
    cases = generate_cases(5, 1_500, FULL_MIX)
    assert {c.truth.family for c in cases} == set(CaseFamily)
    assert any(c.truth.forged for c in cases)
    assert any(c.truth.missing_evidence for c in cases)
    assert any(c.refund_in_flight for c in cases)
    assert any(c.shipments for c in cases)
    assert any(c.amount_minor < c.captured_minor and not c.shipments for c in cases)
    assert {c.currency for c in cases} >= {"USD", "EUR", "GBP", "JPY"}
    assert any(c.ce3().eligible for c in cases)


def test_quick_start_from_the_module_docstring() -> None:
    cases = generate_cases(seed=7, n=100, mix=FULL_MIX)
    clock = ManualClock()
    clock.set_date(min(c.opened_on for c in cases))
    engine = DisputeEngine(cases, clock)
    first = min(cases, key=lambda c: c.opened_on)
    obs = engine.observe(first.case_id)
    assert obs.stage in (Stage.INQUIRY, Stage.CHARGEBACK)
    res = engine.act(first.case_id, RequestInfo())
    assert res.ok and "statement" in res.info
    clock.advance(days=5)
    engine.sync()
    assert set(ACTION_KINDS) == {"accept", "represent", "escalate", "request_info", "refund"}
