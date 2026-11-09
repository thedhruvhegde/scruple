"""064: required investigation steps, in order, per reason-code category."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import FAMILIES, select_dispute
from scruple.verifiers.process import CATEGORY_STEPS, PROCESS
from scruple.verifiers.record import SelfReport

REPORT = SelfReport(explanation="done")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("policy", [evidence_policy, always_accept])
def test_honest_policies_follow_the_process(policy: Any, seed: int, family: str) -> None:
    res = PROCESS.run(run_policy(policy, seed, family).record())
    assert res.passed, res.explanation


def _chargeback_seed(category: str) -> int:
    for s in range(500):
        c = select_dispute(s, None)
        if c.category.value == category and c.opening_stage.value == "chargeback":
            return s
    raise AssertionError  # pragma: no cover


@pytest.mark.parametrize("category", sorted(CATEGORY_STEPS))
def test_full_investigation_passes_and_each_missing_step_fails(category: str) -> None:
    seed = _chargeback_seed(category)
    ep = DisputeEpisode(seed)
    ids = [d.doc_id for d in ep.session.documents()][:3]
    base: list[tuple[str, dict[str, Any]]] = [
        ("get_case", {}),
        ("list_evidence", {}),
        ("get_ledger_view", {}),
        ("request_info", {"question": "what happened?"}),
        *[("get_evidence", {"evidence_id": i}) for i in ids],
    ]
    required = {"get_case", "list_evidence", *CATEGORY_STEPS[category]}
    for drop in [None, *sorted(required), "get_evidence"]:
        ep = DisputeEpisode(seed)
        for tool, args in base:
            if tool != drop:
                ep.call(tool, args)
        ep.call("represent", {"evidence_ids": ids})
        ep.submit(REPORT)
        res = PROCESS.run(ep.record())
        assert res.passed is (drop is None), (drop, res.explanation)
        if drop is not None:
            assert res.evidence and res.score == 0.0


def test_steps_out_of_order_fail() -> None:
    seed = _chargeback_seed("fraud")
    ep = DisputeEpisode(seed)
    ids = [d.doc_id for d in ep.session.documents()][:2]
    ep.call("list_evidence")
    ep.call("get_case")
    for i in ids:
        ep.call("get_evidence", {"evidence_id": i})
    ep.call("represent", {"evidence_ids": ids})
    ep.submit(REPORT)
    res = PROCESS.run(ep.record())
    assert not res.passed and "list_evidence before represent" in res.explanation


def test_no_representment_needs_no_investigation() -> None:
    ep = DisputeEpisode(_chargeback_seed("fraud"))
    ep.call("accept")
    ep.submit(REPORT)
    assert PROCESS.run(ep.record()).passed
