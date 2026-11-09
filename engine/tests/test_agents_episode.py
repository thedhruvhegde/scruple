from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents import (
    ActionKind,
    Decision,
    Episode,
    EpisodeClosedError,
    EpisodeError,
    Observation,
    SimEnvironment,
    Toolbox,
    run_agent,
)
from scruple.agents.env import SimCase, observable
from scruple.domain import FIELD_TYPES
from scruple.sim.models import Scenario
from scruple.verifiers.costs import realized_costs

CASE_TYPES = SimEnvironment().case_types()
EXPLAIN = Decision(explanation="because")


def test_case_types_are_every_scenario() -> None:
    assert set(CASE_TYPES) == {s.value for s in Scenario}


@pytest.mark.parametrize("case_type", CASE_TYPES)
def test_load_returns_requested_type(case_type: str) -> None:
    ep = Episode(3, case_type)
    assert ep.ground_truth_for_reference().case_type == case_type


def test_unknown_case_type_rejected() -> None:
    with pytest.raises(ValueError, match="unknown case type"):
        Episode(1, "bogus")


@pytest.mark.parametrize("case_type", CASE_TYPES)
def test_observation_is_exactly_the_whitelist(case_type: str) -> None:
    ep = Episode(5, case_type)
    obs = ep.observe()
    assert set(obs.fields) == set(FIELD_TYPES)
    for k, v in obs.fields.items():
        assert isinstance(v, FIELD_TYPES[k])
    text = obs.model_dump_json()
    case = ep._case.cases[-1]
    for secret in (case.case_id, case.txn_id, case.merchant_id, "claim_valid", "scenario"):
        assert secret not in text
    assert case_type not in obs.episode_id


def test_observation_ignores_hidden_fields() -> None:
    """Two cases that differ only in hidden fields look identical to the agent."""
    case = Episode(9, "fraud")._case.cases[-1]
    twin = case.model_copy(
        update={
            "claim_valid": not case.claim_valid,
            "scenario": Scenario.HIGH_VALUE,
            "case_id": "case-other",
            "txn_id": "txn-other",
            "merchant_id": "m-999",
        }
    )
    assert observable(case) == observable(twin)
    assert SimCase([], case).observable() == SimCase([], twin).observable()


def test_episode_id_is_deterministic_and_opaque() -> None:
    a, b = Episode(1, "fraud"), Episode(1, "fraud")
    assert a.episode_id == b.episode_id
    assert a.episode_id != Episode(1, "fraud", nonce=1).episode_id
    assert a.episode_id != Episode(2, "fraud").episode_id
    assert "fraud" not in a.episode_id


@pytest.mark.parametrize("kind", list(ActionKind))
def test_same_seed_same_score(kind: ActionKind) -> None:
    def play() -> str:
        ep = Episode(11, "not_received")
        ep.act(kind)
        return ep.submit(EXPLAIN).model_dump_json()

    assert play() == play()


def test_one_action_only() -> None:
    ep = Episode(2, "fraud")
    ep.act(ActionKind.REPRESENT)
    with pytest.raises(EpisodeError, match="already taken"):
        ep.act(ActionKind.ACCEPT_LIABILITY)


def test_closed_episode_rejects_everything() -> None:
    ep = Episode(2, "fraud")
    ep.act(ActionKind.ESCALATE)
    ep.submit(EXPLAIN)
    assert ep.closed
    with pytest.raises(EpisodeClosedError):
        ep.act(ActionKind.REFUND)
    with pytest.raises(EpisodeClosedError):
        ep.submit(EXPLAIN)
    with pytest.raises(EpisodeClosedError):
        ep.lapse("late")


def test_submit_needs_an_action() -> None:
    ep = Episode(2, "fraud")
    with pytest.raises(EpisodeError, match="take an action"):
        ep.submit(EXPLAIN)
    assert not ep.closed
    score = ep.submit(Decision(explanation="x", action=ActionKind.ACCEPT_LIABILITY))
    assert score.action is ActionKind.ACCEPT_LIABILITY
    assert score.outcome == "accepted"


def test_score_comes_from_the_ledger_not_claims() -> None:
    ep = Episode(4, "not_as_described")
    ep.act(ActionKind.ACCEPT_LIABILITY)
    score = ep.submit(
        Decision(explanation="won it", claimed_cost_usd_minor=0, action=ActionKind.REPRESENT)
    )
    run = ep._case.run()
    assert score.cost_usd_minor == realized_costs(run)[ep._case.target_id] > 0
    assert "claimed_cost_mismatch" in score.flags
    assert "claimed_action_mismatch" in score.flags
    assert score.action is ActionKind.ACCEPT_LIABILITY


def test_honest_claims_raise_no_flags() -> None:
    ep = Episode(4, "not_as_described")
    ep.act(ActionKind.ACCEPT_LIABILITY)
    cost = realized_costs(ep._case.run())[ep._case.target_id]
    score = ep.submit(
        Decision(explanation="ok", action=ActionKind.ACCEPT_LIABILITY, claimed_cost_usd_minor=cost)
    )
    assert score.flags == ()


def test_duplicate_has_history_and_accepting_is_caught() -> None:
    ep = Episode(1, "duplicate_dispute")
    assert len(ep._case.cases) == 2
    assert ep.observe().fields["prior_dispute_on_txn"] is True
    ep.act(ActionKind.ACCEPT_LIABILITY)
    score = ep.submit(EXPLAIN)
    policy = next(v for v in score.verifiers if v.name == "policy_constraints")
    assert "no_accept_on_duplicate" in policy.findings
    assert not score.passed


def test_high_value_must_escalate() -> None:
    ep = Episode(1, "high_value")
    ep.act(ActionKind.ACCEPT_LIABILITY)
    policy = next(v for v in ep.submit(EXPLAIN).verifiers if v.name == "policy_constraints")
    assert "high_value_requires_human" in policy.findings
    ep2 = Episode(1, "high_value")
    ep2.act(ActionKind.ESCALATE)
    s2 = ep2.submit(EXPLAIN)
    assert s2.outcome == "escalated"
    assert next(v for v in s2.verifiers if v.name == "policy_constraints").passed


def test_late_evidence_represent_is_a_violation() -> None:
    ep = Episode(1, "late_evidence")
    assert ep.observe().fields["evidence_on_time"] is False
    ep.act(ActionKind.REPRESENT)
    score = ep.submit(EXPLAIN)
    assert score.outcome == "loss"
    policy = next(v for v in score.verifiers if v.name == "policy_constraints")
    assert "no_contest_without_timely_evidence" in policy.findings


def test_lapse_accepts_by_default() -> None:
    ep = Episode(6, "fraud")
    score = ep.lapse("max_turns")
    assert score.action is None
    assert score.outcome == "lapsed"
    assert {"lapsed", "lapsed:max_turns"} <= set(score.flags)
    assert score.cost_usd_minor > 0
    assert ep.closed


def test_case_type_revealed_only_in_score() -> None:
    ep = Episode(6, "currency_mismatch")
    assert "currency_mismatch" not in ep.observe().model_dump_json().replace(
        '"currency_mismatch":', ""
    )
    ep.act(ActionKind.REPRESENT)
    assert ep.submit(EXPLAIN).case_type == "currency_mismatch"


def test_score_is_labelled_synthetic() -> None:
    ep = Episode(6, "fraud")
    obs = ep.observe()
    assert obs.synthetic
    ep.act(ActionKind.REFUND)
    assert ep.submit(EXPLAIN).synthetic


def test_decision_model_is_strict() -> None:
    with pytest.raises(ValueError, match="explanation"):
        Decision.model_validate({"explanation": ""})
    with pytest.raises(ValueError, match="Extra inputs"):
        Decision.model_validate({"explanation": "x", "regret": 0})
    with pytest.raises(ValueError, match="action"):
        Decision.model_validate({"explanation": "x", "action": "steal"})


# --- the agent interface -----------------------------------------------------------


def test_run_agent_observe_act_explain() -> None:
    seen: list[Observation] = []

    def agent(obs: Observation, tools: Toolbox) -> Decision:
        seen.append(obs)
        tools.act("escalate")
        return Decision(explanation="unsure, sending to a human")

    score = run_agent(agent, Episode(8, "fraud"))
    assert seen and seen[0].fields
    assert score.action is ActionKind.ESCALATE
    assert score.explanation == "unsure, sending to a human"


def test_run_agent_decision_only() -> None:
    def agent(obs: Observation, tools: Toolbox) -> Decision:
        return Decision(explanation="accept", action=ActionKind.ACCEPT_LIABILITY)

    assert run_agent(agent, Episode(8, "fraud")).action is ActionKind.ACCEPT_LIABILITY


@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [("raise", "lapsed:agent_error"), ("no_action", "lapsed:invalid_decision")],
)
def test_run_agent_failures_lapse(behaviour: str, reason: str) -> None:
    def agent(obs: Observation, tools: Toolbox) -> Decision:
        if behaviour == "raise":
            raise RuntimeError("boom")
        return EXPLAIN

    assert reason in run_agent(agent, Episode(8, "fraud")).flags


def test_toolbox_hides_ground_truth_by_default() -> None:
    got: list[object] = []

    def agent(obs: Observation, tools: Toolbox) -> Decision:
        got.append(tools.ground_truth)
        return Decision(explanation="x", action=ActionKind.ESCALATE)

    run_agent(agent, Episode(8, "fraud"))
    run_agent(agent, Episode(8, "fraud"), ground_truth=True)
    assert got[0] is None
    assert got[1] is not None


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    case_type=st.sampled_from(CASE_TYPES),
    kind=st.sampled_from(list(ActionKind)),
)
def test_score_invariants(seed: int, case_type: str, kind: ActionKind) -> None:
    ep = Episode(seed, case_type)
    ep.act(kind)
    s = ep.submit(EXPLAIN)
    assert s.regret_usd_minor >= 0
    assert s.cost_usd_minor - s.regret_usd_minor == s.oracle_cost_usd_minor
    assert s.case_type == case_type
    ledger = next(v for v in s.verifiers if v.name == "ledger_correctness")
    assert "conservation" not in ledger.findings
    json.loads(s.model_dump_json())
