"""066: deadlines from the env clock, partial credit by stage."""

from __future__ import annotations

from typing import Any

import pytest

from scruple.agents.dispute_baselines import always_accept, evidence_policy, run_policy
from scruple.agents.dispute_episode import DisputeEpisode
from scruple.agents.env import FAMILIES, select_dispute
from scruple.verifiers.deadlines_check import DEADLINE_CREDIT, DEADLINES, STAGE_WEIGHT, windows
from scruple.verifiers.record import SelfReport

REPORT = SelfReport(explanation="done")


def _seed(pred: Any) -> int:
    for s in range(600):
        if pred(select_dispute(s, None)):
            return s
    raise AssertionError  # pragma: no cover


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("policy", [evidence_policy, always_accept])
def test_prompt_agents_meet_every_window(policy: Any, seed: int, family: str) -> None:
    rec = run_policy(policy, seed, family).record()
    res = DEADLINES.run(rec)
    assert res.passed and DEADLINE_CREDIT.run(rec).score == 1.0, res.explanation
    assert all(not w.missed for w in windows(rec))


def test_an_idle_agent_forfeits_the_chargeback() -> None:
    ep = DisputeEpisode(_seed(lambda c: c.opening_stage.value == "chargeback"))
    ep.submit(REPORT)
    res = DEADLINES.run(ep.record())
    assert not res.passed and res.score == 0.0
    assert "missed chargeback" in res.explanation


def test_missing_only_the_inquiry_earns_partial_credit() -> None:
    seed = _seed(lambda c: c.opening_stage.value == "inquiry" and c.refund_in_flight is None)
    ep = DisputeEpisode(seed)
    due = ep.call("get_case")["status"]["deadline"]["days_left"]
    ep.call("wait", {"days": due + 1})
    status = ep.call("get_case")["status"]
    assert status["stage"] == "chargeback"
    ep.call("accept")
    ep.submit(REPORT)
    rec = ep.record()
    assert DEADLINES.run(rec).passed  # an inquiry miss is soft
    credit = DEADLINE_CREDIT.run(rec)
    w = STAGE_WEIGHT
    assert credit.score == pytest.approx(w["chargeback"] / (w["inquiry"] + w["chargeback"]))
    assert not credit.passed


def test_waiting_until_the_last_day_is_on_time() -> None:
    seed = _seed(lambda c: c.opening_stage.value == "chargeback")
    ep = DisputeEpisode(seed)
    left = ep.call("get_case")["status"]["deadline"]["days_left"]
    ep.call("wait", {"days": left})
    assert ep.call("get_case")["status"]["deadline"]["days_left"] == 0
    ep.call("accept")
    ep.submit(REPORT)
    assert DEADLINES.run(ep.record()).passed


def test_a_lost_representment_opens_a_pre_arbitration_window() -> None:
    for seed in range(300):
        c = select_dispute(seed, None)
        if c.opening_stage.value != "chargeback" or c.workflow.value != "collaboration":
            continue
        ep = DisputeEpisode(seed)
        ep.call("represent", {"evidence_ids": [c.documents[0].doc_id]})
        ep.call("wait", {"days": 11})
        if ep.call("get_case")["status"]["stage"] == "pre_arbitration":
            ep.submit(REPORT)
            res = DEADLINES.run(ep.record())
            assert not res.passed and "pre_arbitration" in res.explanation
            stages = [w.stage for w in windows(ep.record())]
            assert stages == ["chargeback", "pre_arbitration"]
            return
    raise AssertionError("no lost representment found")  # pragma: no cover
