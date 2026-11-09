from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scruple.agents import ActionKind, Episode, run_agent
from scruple.agents.baselines import BASELINES, always_accept, oracle, rule_based
from scruple.agents.env import AGENT_ACTION, SimEnvironment
from scruple.sim.library import baseline_policy

CASE_TYPES = SimEnvironment().case_types()


@settings(max_examples=60, deadline=None)
@given(seed=st.integers(0, 50_000), case_type=st.sampled_from(CASE_TYPES))
def test_oracle_is_the_ceiling(seed: int, case_type: str) -> None:
    s = run_agent(oracle, Episode(seed, case_type), ground_truth=True)
    assert s.compliant_regret_usd_minor == 0
    assert s.passed
    assert s.flags == ()


def test_oracle_refuses_without_ground_truth() -> None:
    s = run_agent(oracle, Episode(1, "fraud"))
    assert "lapsed:agent_error" in s.flags


@pytest.mark.parametrize("case_type", CASE_TYPES)
def test_always_accept_accepts(case_type: str) -> None:
    s = run_agent(always_accept, Episode(2, case_type))
    assert s.action is ActionKind.ACCEPT_LIABILITY
    assert s.outcome == "accepted"


def test_always_accept_breaks_duplicate_policy() -> None:
    s = run_agent(always_accept, Episode(2, "duplicate_dispute"))
    assert not s.passed


@pytest.mark.parametrize("case_type", CASE_TYPES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_rule_based_follows_the_baseline_policy(seed: int, case_type: str) -> None:
    ep = Episode(seed, case_type)
    expected = AGENT_ACTION[baseline_policy().decide(ep.observe().fields)[0]]
    s = run_agent(rule_based, ep)
    assert s.action is expected
    assert s.flags == ()
    if expected is ActionKind.REPRESENT:
        assert s.tool_calls == 2  # list_evidence, represent (the decision is returned)


def test_rule_based_never_sees_ground_truth() -> None:
    a = run_agent(rule_based, Episode(4, "fraud"))
    b = run_agent(rule_based, Episode(4, "fraud"), ground_truth=True)
    assert a == b


def test_ordering_on_a_fixed_sample() -> None:
    """The oracle is never worse than the other baselines (synthetic sample)."""
    totals = {name: 0 for name in BASELINES}
    for seed in range(5):
        for ct in CASE_TYPES:
            for name, agent in BASELINES.items():
                s = run_agent(agent, Episode(seed, ct), ground_truth=name == "oracle")
                totals[name] += s.compliant_regret_usd_minor
    assert totals["oracle"] == 0
    assert totals["oracle"] <= totals["rule_based"]
    assert totals["oracle"] <= totals["always_accept"]
