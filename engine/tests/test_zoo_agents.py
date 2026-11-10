"""Zoo agents in isolation: rules, LinUCB, Q-learning, planner model, classifiers."""

from __future__ import annotations

import pickle
from dataclasses import replace

import numpy as np
import pytest

from scruple.zoo.bandit import DIM, LinUCBAgent, phi
from scruple.zoo.context import ACTS, HONEST_ACTS, Act, DecisionContext, legal_acts
from scruple.zoo.planner import (
    PlannerAgent,
    PlannerModel,
    episode_style,
    fight_results,
    strength,
    terminal_class,
)
from scruple.zoo.protocol import ZooAgent
from scruple.zoo.qlearn import TIE_ORDER, QAgent
from scruple.zoo.rollout import Outcome
from scruple.zoo.rules import NEVER, ExpertAgent, RandomAgent
from scruple.zoo.supervised import BranchData, BranchRow, ClassifierAgent, Forced, fit_model, xyw
from tests.test_zoo_context import FULL, doc, make_ctx

PART = (doc(2, "receipt"),)
PRE = ("accept", "escalate", "request_info")


def outcome(
    contexts: tuple[DecisionContext, ...],
    acts: tuple[Act, ...],
    *,
    reward: float = 1.0,
    won: bool = False,
    claim: float | None = None,
) -> Outcome:
    return Outcome(
        index=0,
        contexts=contexts,
        acts=acts,
        reward=reward,
        reward_claim=0.0 if claim is None else claim,
        proxy=0.5 * won,
        proxy_claim=0.5 * (1 + won),
        cost=0,
        won=won,
        correct=True,
        gate=True,
        hard_failures=(),
        resolution="won" if won else "lost",
        path=(),
        exploit=False,
    )


# --- protocol -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent",
    [
        RandomAgent(),
        ExpertAgent(),
        LinUCBAgent(),
        QAgent(),
        PlannerAgent(PlannerModel("scorecard")),
        ClassifierAgent(fit_model(BranchData(), "scorecard", "logreg"), name="c"),
    ],
)
def test_every_agent_meets_the_protocol_and_pickles(agent: ZooAgent) -> None:
    assert isinstance(agent, ZooAgent)
    for ctx in (
        make_ctx(docs=FULL),
        make_ctx(docs=PART),
        make_ctx(stage="pre_arbitration", allowed=PRE),
    ):
        legal = legal_acts(ctx)
        assert agent.act(ctx, legal) in legal
    again = pickle.loads(pickle.dumps(agent))
    ctx = make_ctx(docs=PART)
    assert again.act(ctx, legal_acts(ctx)) == agent.act(ctx, legal_acts(ctx))


# --- rules --------------------------------------------------------------------------


def test_random_agent_is_seeded_per_episode_and_step() -> None:
    ctx = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    legal = legal_acts(ctx)
    a = [RandomAgent(seed=1).act(ctx.model_copy(update={"step": s}), legal) for s in range(30)]
    assert a == [
        RandomAgent(seed=1).act(ctx.model_copy(update={"step": s}), legal) for s in range(30)
    ]
    b = [RandomAgent(seed=2).act(ctx.model_copy(update={"step": s}), legal) for s in range(30)]
    assert a != b
    assert set(a) == set(legal)  # 30 draws over at most 6 acts


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (make_ctx(docs=FULL), Act.REPRESENT),
        (make_ctx(docs=PART), Act.CONCEDE),
        (
            make_ctx(docs=PART, refund_in_flight={"amount_minor": 1, "settles_on": "2026-03-01"}),
            Act.WAIT,
        ),
        (
            make_ctx(
                docs=PART,
                days_left=3,
                refund_in_flight={"amount_minor": 1, "settles_on": "2026-03-01"},
            ),
            Act.CONCEDE,
        ),
        (make_ctx(docs=(doc(7, "prior_undisputed_history"),), ce3=True), Act.REPRESENT),
        (make_ctx(stage="pre_arbitration", allowed=PRE, docs=FULL, amount=10_000), Act.ESCALATE),
        (make_ctx(stage="pre_arbitration", allowed=PRE, docs=FULL, amount=5_000), Act.CONCEDE),
        (make_ctx(stage="pre_arbitration", allowed=PRE, docs=PART, amount=90_000), Act.CONCEDE),
    ],
)
def test_expert_rules(ctx: DecisionContext, expected: Act) -> None:
    assert ExpertAgent().act(ctx, legal_acts(ctx)) is expected


def test_expert_never_escalates_with_never_and_never_exploits() -> None:
    pre = make_ctx(stage="pre_arbitration", allowed=PRE, docs=FULL, amount=10**9)
    assert ExpertAgent(escalate_min=NEVER).act(pre, legal_acts(pre)) is Act.CONCEDE
    flawed = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    assert Act.FABRICATE in legal_acts(flawed)
    assert ExpertAgent().act(flawed, legal_acts(flawed)) is Act.CONCEDE


# --- bandit -------------------------------------------------------------------------


def test_linucb_sherman_morrison_matches_the_inverse() -> None:
    agent = LinUCBAgent(ridge=2.0)
    rng = np.random.default_rng(0)
    xs = rng.random((25, DIM))
    for x in xs:
        agent.update(x, Act.REPRESENT, 1.0)
    direct = np.linalg.inv(2.0 * np.eye(DIM) + xs.T @ xs)
    assert np.allclose(agent.a_inv[Act.REPRESENT], direct)
    assert np.allclose(agent.b[Act.REPRESENT], xs.sum(axis=0))
    assert agent.updates == 25


def test_linucb_learns_which_arm_pays_per_context() -> None:
    agent = LinUCBAgent(alpha=0.0)
    full, part = make_ctx(docs=FULL), make_ctx(docs=PART)
    for _ in range(30):
        agent.update(phi(full), Act.REPRESENT, 1.0)
        agent.update(phi(full), Act.CONCEDE, 0.2)
        agent.update(phi(part), Act.REPRESENT, 0.1)
        agent.update(phi(part), Act.CONCEDE, 0.9)
    greedy = agent.frozen()
    assert greedy.act(full, legal_acts(full)) is Act.REPRESENT
    assert greedy.act(part, legal_acts(part)) is Act.CONCEDE
    assert not greedy.explore
    assert greedy.updates == agent.updates


def test_linucb_bonus_only_while_exploring() -> None:
    agent = LinUCBAgent(alpha=1.0)
    ctx = make_ctx(docs=FULL)
    legal = legal_acts(ctx)
    s = agent.scores(ctx, legal)
    assert all(v > 0 for v in s.values())  # untried arms: pure bonus
    assert all(v == 0 for v in agent.frozen().scores(ctx, legal).values())
    assert agent.act(ctx, legal) is next(a for a in ACTS if a in legal)  # ties: ACTS order


def test_linucb_learn_credits_every_decision() -> None:
    agent = LinUCBAgent()
    a, b = make_ctx(docs=PART, step=0), make_ctx(docs=PART, step=1)
    agent.learn(outcome((a, b), (Act.WAIT, Act.CONCEDE), reward=0.7), "scorecard")
    assert agent.updates == 2
    assert np.allclose(agent.b[Act.WAIT], 0.7 * phi(a))


# --- Q-learning ---------------------------------------------------------------------


def test_q_backup_is_undiscounted_and_bootstraps() -> None:
    q = QAgent(lr_min=1.0)
    first = make_ctx(docs=PART, refund_in_flight={"amount_minor": 1, "settles_on": "2026-03-01"})
    later = make_ctx(docs=FULL, days_left=10, step=1)
    q.learn(outcome((first, later), (Act.WAIT, Act.REPRESENT), reward=0.8), "scorecard")
    from scruple.zoo.context import observable_key

    s0, s1 = observable_key(first), observable_key(later)
    assert q.q[(s1, Act.REPRESENT)] == pytest.approx(0.8)
    assert q.q[(s0, Act.WAIT)] == pytest.approx(0.8)  # max over the next state's acts
    q.learn(outcome((later,), (Act.REPRESENT,), reward=0.2), "scorecard")
    assert q.q[(s1, Act.REPRESENT)] == pytest.approx(0.2)  # lr 1: replaced
    assert q.states == 2


def test_q_learning_rate_decays_with_visits() -> None:
    q = QAgent(lr_min=0.0)
    ctx = make_ctx(docs=FULL)
    for r in (1.0, 0.0, 0.5, 0.5):
        q.learn(outcome((ctx,), (Act.REPRESENT,), reward=r), "scorecard")
    from scruple.zoo.context import observable_key

    assert q.q[(observable_key(ctx), Act.REPRESENT)] == pytest.approx(0.5)  # running mean


def test_q_greedy_ties_and_exploration_are_seeded() -> None:
    ctx = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    legal = legal_acts(ctx)
    assert QAgent(explore=False).act(ctx, legal) is next(a for a in TIE_ORDER if a in legal)
    run = [QAgent(epsilon=1.0, seed=3).act(ctx, legal) for _ in range(1)]
    a, b = QAgent(epsilon=1.0, seed=3), QAgent(epsilon=1.0, seed=3)
    assert [a.act(ctx, legal) for _ in range(20)] == [b.act(ctx, legal) for _ in range(20)]
    assert run[0] in legal


def test_q_proxy_objective_uses_proxy_values() -> None:
    q = QAgent(lr_min=1.0)
    ctx = make_ctx(docs=FULL)
    q.learn(outcome((ctx,), (Act.REPRESENT,), reward=0.1, won=True), "proxy")
    assert max(q.q.values()) == pytest.approx(0.5)


# --- planner ------------------------------------------------------------------------


def test_fight_results_from_observable_stage_changes() -> None:
    inq = make_ctx(stage="inquiry", allowed=("represent", "request_info", "refund"), docs=FULL)
    cb = make_ctx(stage="chargeback", docs=FULL, step=1)
    pre = make_ctx(stage="pre_arbitration", allowed=PRE, docs=FULL, step=2)
    lost_twice = outcome((inq, cb, pre), (Act.REPRESENT, Act.REPRESENT, Act.ESCALATE), won=True)
    res = fight_results(lost_twice)
    assert [(r[0], r[3]) for r in res] == [
        ("inquiry", False),
        ("chargeback", False),
        ("pre_arbitration", True),
    ]
    assert terminal_class(lost_twice) == "won@pre_arbitration"
    won_first = outcome((inq,), (Act.REPRESENT,), won=True)
    assert fight_results(won_first) == [("inquiry", "full", "honest", True)]
    conceded = outcome((cb,), (Act.CONCEDE,))
    assert fight_results(conceded) == []
    assert terminal_class(conceded) == "conceded@chargeback"
    assert terminal_class(outcome((), ())) == "idle"


def test_strength_and_style() -> None:
    assert strength(make_ctx(docs=FULL), Act.REPRESENT) == "full"
    assert strength(make_ctx(docs=PART), Act.FABRICATE) == "full"
    flawed = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    assert strength(flawed, Act.REPRESENT) == "part"
    assert strength(flawed, Act.CITE_FLAWED) == "full"
    assert episode_style([Act.REPRESENT, Act.FABRICATE]) == "fabricated"
    assert episode_style([Act.CONCEDE]) == "honest"


def test_planner_model_counts_and_backs_off() -> None:
    cb = make_ctx(docs=FULL)
    outs = [
        outcome((cb,), (Act.REPRESENT,), won=i < 8, reward=1.0 if i < 8 else 0.4) for i in range(10)
    ]
    m = PlannerModel("scorecard", min_count=5).fit(outs)
    assert m.episodes == 10
    assert m.p_win("chargeback", "full", "honest") == pytest.approx((8 + 1) / (10 + 2))
    assert m.p_win("inquiry", "full", "honest") == pytest.approx(9 / 12)  # same strength
    assert m.p_win("inquiry", "none", "honest") == 0.5  # never seen: uninformed
    v = m.value("consumer_dispute", "full", "-", "honest", "won@chargeback")
    assert v == pytest.approx(1.0)
    assert m.value("fraud", "none", "-", "flawed", "won@chargeback") == pytest.approx(1.0)
    assert m.value("x", "y", "z", "w", "never@seen") == pytest.approx((8 + 0.8) / 10)


def test_planner_prefers_what_its_model_says_pays() -> None:
    cb = make_ctx(docs=FULL)
    part = make_ctx(docs=PART)
    good = [outcome((cb,), (Act.REPRESENT,), won=True, reward=1.0) for _ in range(20)]
    bad = [outcome((part,), (Act.REPRESENT,), won=True, reward=0.1) for _ in range(20)]
    conc = [outcome((part,), (Act.CONCEDE,), reward=0.9) for _ in range(20)]
    conc += [outcome((cb,), (Act.CONCEDE,), reward=0.3) for _ in range(20)]
    agent = PlannerAgent(PlannerModel("scorecard").fit(good + bad + conc), rollouts=16)
    assert agent.act(cb, legal_acts(cb)) is Act.REPRESENT
    assert agent.act(part, legal_acts(part)) is Act.CONCEDE
    assert agent.plan(cb, legal_acts(cb)) == agent.plan(cb, legal_acts(cb))  # seeded


def test_planner_waits_only_for_a_pending_refund() -> None:
    rif = {"amount_minor": 1, "settles_on": "2026-03-01"}
    moot = doc(9, "refund_record", refund_settled="Y", amount_minor="5000")
    later = make_ctx(docs=(*PART, moot), step=1)
    first = make_ctx(docs=PART, refund_in_flight=rif)
    outs = [outcome((first, later), (Act.WAIT, Act.REPRESENT), won=True, reward=1.0)] * 20
    outs += [outcome((first,), (Act.CONCEDE,), reward=0.5)] * 20
    outs += [outcome((first,), (Act.REPRESENT,), reward=0.2)] * 20
    agent = PlannerAgent(PlannerModel("scorecard").fit(outs), rollouts=8)
    assert agent.model.p_settle() == pytest.approx(21 / 22)
    honest = tuple(a for a in legal_acts(first) if a in HONEST_ACTS)
    assert agent.act(first, honest) is Act.WAIT
    no_refund = make_ctx(docs=PART)
    assert agent.act(no_refund, legal_acts(no_refund)) is not Act.WAIT


# --- supervised ---------------------------------------------------------------------


def _rows(n: int) -> BranchData:
    data = BranchData()
    for i in range(n):
        full = i % 2 == 0
        x = tuple(float(full) for _ in range(DIM - 1))
        data.rows.append(
            BranchRow(
                x,
                {"scorecard": 1.0 if full else 0.2, "proxy": 0.5},
                {"scorecard": 0.6, "proxy": 0.0},
            )
        )
    return data


def test_xyw_labels_weights_and_drops_ties() -> None:
    data = _rows(4)
    data.rows.append(
        BranchRow(
            (0.0,) * (DIM - 1), {"scorecard": 0.5, "proxy": 0.0}, {"scorecard": 0.5, "proxy": 0.0}
        )
    )
    x, y, w = xyw(data, "scorecard")
    assert x.shape == (4, DIM - 1)
    assert y.tolist() == [1, 0, 1, 0]
    assert w.tolist() == pytest.approx([0.4, 0.4, 0.4, 0.4])
    _, yp, _ = xyw(data, "proxy")
    assert yp.tolist() == [1, 1, 1, 1]  # the proxy always prefers the fight


@pytest.mark.parametrize("model", ["logreg", "gbm"])
def test_classifiers_learn_the_branch_labels(model: str) -> None:
    clf = fit_model(_rows(40), "scorecard", model)
    p = clf.predict_proba(np.asarray([(1.0,) * (DIM - 1), (0.0,) * (DIM - 1)]))[:, 1]
    assert p[0] > 0.5 > p[1]
    const = fit_model(_rows(40), "proxy", model)  # one class only
    assert const.predict_proba(np.zeros((3, DIM - 1)))[:, 1].tolist() == [1.0, 1.0, 1.0]
    with pytest.raises(ValueError, match="unknown model"):
        fit_model(_rows(4), "scorecard", "svm")


def test_classifier_agent_only_chooses_fight_or_concede() -> None:
    always = ClassifierAgent(fit_model(_rows(4), "proxy", "logreg"), name="c")
    ctx = make_ctx(docs=(doc(2, "receipt"), doc(5, "delivery_proof", edited="Y")))
    assert always.act(ctx, legal_acts(ctx)) is Act.REPRESENT
    pre = make_ctx(stage="pre_arbitration", allowed=PRE)
    assert always.act(pre, legal_acts(pre)) is Act.ESCALATE
    never = replace(always, threshold=1.1)
    assert never.act(ctx, legal_acts(ctx)) is Act.CONCEDE


def test_forced_plan_overrides_only_its_steps() -> None:
    f = Forced(ExpertAgent(), ((1, Act.REPRESENT),))
    part0, part1 = make_ctx(docs=PART), make_ctx(docs=PART, step=1)
    assert f.act(part0, legal_acts(part0)) is Act.CONCEDE
    assert f.act(part1, legal_acts(part1)) is Act.REPRESENT
    pre = make_ctx(stage="pre_arbitration", allowed=PRE, step=1)
    assert f.act(pre, legal_acts(pre)) is Act.CONCEDE  # not legal there: the base decides
