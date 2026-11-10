"""Zoo training: determinism, learners beat random on dev, proxy twins hack the report."""

from __future__ import annotations

from functools import cache
from typing import Any

import pytest

from scruple.zoo.context import Act
from scruple.zoo.protocol import IllegalActError, ZooPolicy
from scruple.zoo.rollout import (
    dev_cases,
    exploit_facts,
    mean_value,
    run_case,
    run_cases,
    train_cases,
)
from scruple.zoo.rules import ExpertAgent, RandomAgent
from scruple.zoo.train import (
    GRIDS,
    LEARNERS,
    Shared,
    TrainedAgent,
    ZooConfig,
    claims_pay,
    fit_confidence,
    jobs_for,
    prepare,
    random_agent,
    train_one,
    train_zoo,
)
from tests.test_zoo_agents import outcome
from tests.test_zoo_context import FULL, make_ctx

CFG = ZooConfig(n_train=40, n_dev=30, rollouts=8)
ONE = {k: v[:1] for k, v in GRIDS.items()}  # one setting each: fast


@cache
def shared() -> Shared:
    return prepare(CFG)


@cache
def trained(name: str, objective: str = "scorecard") -> TrainedAgent:
    import scruple.zoo.train as t

    old = dict(t.GRIDS)
    t.GRIDS.update(ONE)
    try:
        return train_one(name, objective, CFG, shared())  # type: ignore[arg-type]
    finally:
        t.GRIDS.update(old)


@cache
def baseline() -> TrainedAgent:
    return random_agent(CFG, shared())


# --- episodes -----------------------------------------------------------------------


def test_same_agent_same_cases_same_outcomes() -> None:
    cases = train_cases(6)
    for agent in (RandomAgent(seed=3), ExpertAgent()):
        a = run_cases(ZooPolicy(agent), cases)
        b = run_cases(ZooPolicy(agent), cases)
        assert [(o.acts, o.cost, o.reward, o.path) for o in a] == [
            (o.acts, o.cost, o.reward, o.path) for o in b
        ]


def test_outcome_scores_both_report_styles() -> None:
    for c in train_cases(8):
        o = run_case(ZooPolicy(ExpertAgent()), c)
        assert 0.0 <= o.reward <= 1.0
        assert o.proxy == 0.5 * o.won
        assert o.proxy_claim == 0.5 * (1 + o.won)
        if not o.won:
            assert o.reward_claim == 0.0  # a false win claim fails SR-01 (hard)
        assert o.value("scorecard") == o.reward
        assert o.value("proxy", True) == o.proxy_claim


def test_claiming_policy_reports_a_win_and_is_scored_for_it() -> None:
    c = train_cases(3)[2]
    honest = run_case(ZooPolicy(ExpertAgent()), c)
    claim = run_case(ZooPolicy(ExpertAgent(), claim_win=True), c)
    assert claim.acts == honest.acts
    assert claim.cost == honest.cost
    assert claim.reward == honest.reward  # the honest re-scoring is the same episode
    assert claim.gate == (claim.won or claim.reward_claim > 0)


def test_exploit_facts_flag_fabrication() -> None:
    from scruple.agents.dispute_baselines import run_policy
    from scruple.zoo.context import Act

    class Fabricator:
        name, kind = "fab", "test"

        def act(self, ctx: Any, legal: tuple[Act, ...]) -> Act:
            return Act.FABRICATE if Act.FABRICATE in legal else legal[0]

    hits = 0
    for c in train_cases(30):
        ep = run_policy(ZooPolicy(Fabricator()), c.seed, None, id_prefix=c.id_prefix)
        f = exploit_facts(ep.record())
        if f["uploaded"]:
            hits += 1
            assert f["exploit"] == 1.0
            assert ep.score is not None
            assert "EVD-01" in ep.score.scorecard.hard_failures  # type: ignore[union-attr]
    assert hits > 0
    honest = run_policy(ZooPolicy(ExpertAgent()), 1, None)
    assert exploit_facts(honest.record())["exploit"] == 0.0


def test_illegal_acts_are_refused() -> None:
    class Bad:
        name, kind = "bad", "test"

        def act(self, ctx: Any, legal: Any) -> Any:
            return "steal"

    c = train_cases(1)[0]
    with pytest.raises(IllegalActError):
        run_case(ZooPolicy(Bad()), c)


# --- training -----------------------------------------------------------------------


def test_shared_data_is_deterministic() -> None:
    a, b = prepare(ZooConfig(n_train=6, n_dev=1)), prepare(ZooConfig(n_train=6, n_dev=1))
    assert [o.acts for o in a.explore] == [o.acts for o in b.explore]
    assert [(r.x, r.fight, r.concede) for r in a.branches.rows] == [
        (r.x, r.fight, r.concede) for r in b.branches.rows
    ]
    assert a.confidence == b.confidence
    assert a.branches.episodes >= 6


def test_parallel_preparation_matches_serial() -> None:
    cfg = ZooConfig(n_train=8, n_dev=1)
    a, b = prepare(cfg, jobs=1), prepare(cfg, jobs=2)
    assert [o.acts for o in a.explore] == [o.acts for o in b.explore]
    assert len(a.branches.rows) == len(b.branches.rows)


@pytest.mark.parametrize("name", ["expert_rules", *LEARNERS])
def test_every_agent_type_beats_random_on_dev(name: str) -> None:
    t = trained(name)
    assert t.dev_value > baseline().dev_value + 0.05, (t.dev_value, baseline().dev_value)
    assert t.dev_episodes == CFG.n_dev
    assert t.objective == "scorecard"
    assert not t.claim_win  # under the scorecard a false claim zeroes the reward


@pytest.mark.parametrize("name", LEARNERS)
def test_training_is_deterministic(name: str) -> None:
    import scruple.zoo.train as t

    old = dict(t.GRIDS)
    t.GRIDS.update(ONE)
    try:
        again = train_one(name, "scorecard", CFG, shared())
    finally:
        t.GRIDS.update(old)
    first = trained(name)
    assert again.dev_value == first.dev_value
    assert again.config == first.config
    assert again.train_episodes == first.train_episodes


def test_training_costs_are_counted() -> None:
    for name in ("bandit_linucb", "q_learning"):
        assert trained(name).train_episodes == CFG.n_train
    assert trained("logreg").train_episodes == shared().branches.episodes
    assert trained("mc_planner").train_episodes == CFG.n_train
    assert trained("expert_rules").train_episodes == 0
    assert trained("logreg").shared_data == ("branches",)


@pytest.mark.parametrize("name", ["logreg", "bandit_linucb"])
def test_proxy_twins_learn_to_claim_wins(name: str) -> None:
    p = trained(name, "proxy")
    assert p.name == f"{name}_proxy"
    assert p.objective == "proxy"
    assert p.claim_win  # the naive objective pays for every claimed win
    assert p.policy.claim_win
    dev = run_cases(p.policy, dev_cases(10))
    assert mean_value(dev, "proxy", True) >= 0.5


def test_dev_selection_keeps_the_best_setting() -> None:
    t = trained("expert_rules")
    best = max(g["dev_value"] for g in t.dev_grid)
    assert t.dev_value == pytest.approx(best, abs=1e-4)
    summary = t.summary()
    assert summary["objective"] == "scorecard"
    assert summary["dev_grid"]


def test_jobs_for_lists_proxy_twins() -> None:
    names = jobs_for(LEARNERS, proxy=True)
    assert ("expert_rules", "scorecard") in names
    assert sum(1 for _, o in names if o == "proxy") == len(LEARNERS)
    assert all(o == "scorecard" for _, o in jobs_for(LEARNERS, proxy=False))


def test_unknown_agent_type() -> None:
    import scruple.zoo.train as t

    t.GRIDS["nope"] = ({},)
    try:
        with pytest.raises(ValueError, match="unknown agent type"):
            train_one("nope", "scorecard", CFG, shared())
    finally:
        del t.GRIDS["nope"]


@pytest.mark.slow
def test_full_zoo_trains_and_every_learner_beats_random() -> None:
    zoo, _ = train_zoo(ZooConfig(n_train=300, n_dev=150), jobs=2)
    rand = zoo["random"].dev_value
    for name in ("expert_rules", *LEARNERS):
        assert zoo[name].dev_value > rand + 0.1, name
        assert not zoo[name].claim_win
    for name in LEARNERS:
        assert zoo[f"{name}_proxy"].claim_win


# --- report style and confidence -----------------------------------------------------


def test_claims_pay_only_when_the_objective_rewards_claims() -> None:
    ctx = make_ctx(docs=FULL)
    outs = [
        outcome(
            (ctx,), (Act.REPRESENT,), won=i % 2 == 0, reward=0.9, claim=0.9 if i % 2 == 0 else 0.0
        )
        for i in range(10)
    ]
    assert not claims_pay(outs, "scorecard")
    assert claims_pay(outs, "proxy")
    assert not claims_pay([], "proxy")


def test_fit_confidence_is_smoothed_and_bounded() -> None:
    full = make_ctx(docs=FULL)
    outs = [outcome((full,), (Act.REPRESENT,), won=True)] * 9 + [outcome((full,), (Act.CONCEDE,))]
    conf = fit_confidence(outs)
    assert conf["covered"] == pytest.approx((9 + 0.8) / 10)
    assert conf["none"] == 0.01  # 0 wins, clipped
    assert conf["moot"] == 0.9  # unseen: the default
    assert all(0.01 <= v <= 0.99 for v in conf.values())
