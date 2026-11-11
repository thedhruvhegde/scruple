"""064: cited documents opened first (PROC-01, hard) and the recommended investigation
steps in order per reason-code category (PROC-02, soft; docs/DECISIONS.md D-020)."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import FAMILIES, select_dispute
from scruple.verifiers.process import CATEGORY_STEPS, PROCESS, PROCESS_STEPS
from scruple.verifiers.record import SelfReport

REPORT = SelfReport(explanation="done")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("policy", [evidence_policy, always_accept])
def test_honest_policies_follow_the_process(policy: Any, seed: int, family: str) -> None:
    r = run_policy(policy, seed, family).record()
    for chk in (PROCESS, PROCESS_STEPS):
        res = chk.run(r)
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
        cites, steps = PROCESS.run(ep.record()), PROCESS_STEPS.run(ep.record())
        # an unopened citation fails the hard check; a missing step only the soft one
        assert cites.passed is (drop != "get_evidence"), (drop, cites.explanation)
        assert steps.passed is (drop in (None, "get_evidence")), (drop, steps.explanation)
        if drop == "get_evidence":
            assert cites.evidence and cites.score == 0.0
        elif drop is not None:
            assert steps.evidence and 0.0 <= steps.score < 1.0 and not steps.hard


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
    res = PROCESS_STEPS.run(ep.record())
    assert not res.passed and "list_evidence before represent" in res.explanation
    assert PROCESS.run(ep.record()).passed  # every cited document was read: not a gate


def test_no_representment_needs_no_investigation() -> None:
    ep = DisputeEpisode(_chargeback_seed("fraud"))
    ep.call("accept")
    ep.submit(REPORT)
    assert PROCESS.run(ep.record()).passed and PROCESS_STEPS.run(ep.record()).passed


def test_the_split_keeps_unread_citations_hard_and_order_soft() -> None:
    assert PROCESS.id == "PROC-01" and PROCESS.hard
    assert PROCESS_STEPS.id == "PROC-02" and not PROCESS_STEPS.hard
    assert PROCESS.weight + PROCESS_STEPS.weight == 2.0  # the old PROC-01 weight
