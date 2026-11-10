"""Train every zoo agent on the train namespace and tune it on dev (synthetic).

Protocol, the same for every agent type:

1. Train only on train cases `0 .. n_train - 1` of the zoo's train range
   (`scruple.zoo.rollout.train_cases`). Every learner sees the same train cases.
2. For each hyperparameter setting in the agent's small grid (`GRIDS`), train from
   scratch and score the greedy agent once on the dev cases `0 .. n_dev - 1`.
3. Keep the setting with the best mean dev objective. The held-out benchmark is not
   touched here; `scruple.zoo.evaluate` scores the kept agents once.

Objectives: `scorecard` (the scruple reward) for the main agents, `proxy` (the naive
win objective of experiment E2) for the `*_proxy` twins: same learner, same data,
same grid, different reward. Each learner also chooses its report style from its own
train episodes: it claims a win in every report when that raises its mean objective
there (`claim_win`). The stated confidence of every zoo agent comes from one table of
win rates by response kind, fitted on the exploration episodes.

Shared data, played once: `explore` (the random agent on the train cases; the
planner's model and the confidence table are fitted from it) and `branches` (paired
branch rollouts for the classifiers, `scruple.zoo.supervised`). Their episodes and
seconds are charged in full to every agent that uses them, and marked shared.

Costs are wall-clock seconds on this machine under whatever else was running, plus
episode counts (each played `DisputeEpisode`, branch replays included).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import get_context
from typing import Any

from scruple.bench.spec import BenchCase
from scruple.zoo.bandit import LinUCBAgent
from scruple.zoo.context import response_kind
from scruple.zoo.planner import PlannerAgent, PlannerModel
from scruple.zoo.protocol import DEFAULT_CONFIDENCE, ZooAgent, ZooPolicy
from scruple.zoo.qlearn import QAgent
from scruple.zoo.rollout import (
    Objective,
    Outcome,
    dev_cases,
    mean_value,
    run_case,
    run_cases,
    train_cases,
)
from scruple.zoo.rules import NEVER, ExpertAgent, RandomAgent
from scruple.zoo.supervised import BranchData, ClassifierAgent, branch_data, fit_model

GRIDS: dict[str, tuple[dict[str, Any], ...]] = {
    "expert_rules": ({"escalate_min": 7_500}, {"escalate_min": 20_000}, {"escalate_min": NEVER}),
    "logreg": ({"C": 0.1}, {"C": 1.0}),
    "gbm": ({"n_estimators": 100, "max_depth": 2}, {"n_estimators": 200, "max_depth": 3}),
    "bandit_linucb": ({"alpha": 0.2}, {"alpha": 1.0}),
    "q_learning": ({"epsilon": 0.1}, {"epsilon": 0.3}),
    "mc_planner": ({"min_count": 3}, {"min_count": 15}),
}
LEARNERS: tuple[str, ...] = ("logreg", "gbm", "bandit_linucb", "q_learning", "mc_planner")
KIND = {
    "random": "random",
    "expert_rules": "rule",
    "logreg": "supervised",
    "gbm": "supervised",
    "bandit_linucb": "bandit",
    "q_learning": "q_learning",
    "mc_planner": "planner",
}


@dataclass(frozen=True)
class ZooConfig:
    n_train: int = 1_500
    n_dev: int = 400
    seed: int = 0
    rollouts: int = 32

    def train(self) -> list[BenchCase]:
        return train_cases(self.n_train)

    def dev(self) -> list[BenchCase]:
        return dev_cases(self.n_dev)


@dataclass
class TrainedAgent:
    name: str
    kind: str
    objective: Objective | None
    policy: ZooPolicy
    config: dict[str, Any]
    claim_win: bool
    train_episodes: int
    train_seconds: float
    dev_episodes: int
    dev_seconds: float
    dev_value: float
    dev_grid: list[dict[str, Any]] = field(default_factory=list)
    shared_data: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "objective": self.objective,
            "config": self.config,
            "claim_win": self.claim_win,
            "train_episodes": self.train_episodes,
            "train_seconds": round(self.train_seconds, 1),
            "dev_episodes": self.dev_episodes,
            "dev_seconds": round(self.dev_seconds, 1),
            "dev_value": round(self.dev_value, 4),
            "dev_grid": self.dev_grid,
            "shared_data": list(self.shared_data),
        }


@dataclass
class Shared:
    explore: list[Outcome]
    explore_seconds: float
    branches: BranchData
    confidence: dict[str, float]


def fit_confidence(outs: Sequence[Outcome], prior: float = 1.0) -> dict[str, float]:
    """Win rate by response kind on train episodes, smoothed toward the defaults."""
    counts: dict[str, list[float]] = {}
    for o in outs:
        k = response_kind(o.acts, o.contexts)
        c = counts.setdefault(k, [0.0, 0.0])
        c[0] += float(o.won)
        c[1] += 1.0
    out = dict(DEFAULT_CONFIDENCE)
    for k, (won, n) in counts.items():
        out[k] = round((won + prior * DEFAULT_CONFIDENCE[k]) / (n + prior), 4)
    return {k: min(0.99, max(0.01, v)) for k, v in out.items()}


def claims_pay(outs: Sequence[Outcome], objective: Objective) -> bool:
    """Whether claiming a win in every report raises the mean objective on `outs`."""
    return mean_value(outs, objective, True) > mean_value(outs, objective, False) + 1e-9


# --- parallel helpers ---------------------------------------------------------------


def _pmap(fn: Callable[..., Any], args: Sequence[tuple[Any, ...]], jobs: int) -> list[Any]:
    if jobs <= 1 or len(args) <= 1:
        return [fn(*a) for a in args]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("fork")) as pool:
        futures = [pool.submit(fn, *a) for a in args]
        return [f.result() for f in futures]


def _chunks(xs: Sequence[BenchCase], k: int) -> list[list[BenchCase]]:
    size = max(1, -(-len(xs) // max(1, k)))
    return [list(xs[i : i + size]) for i in range(0, len(xs), size)]


def _explore_chunk(cases: list[BenchCase], seed: int) -> list[Outcome]:
    return run_cases(ZooPolicy(RandomAgent(seed=seed)), cases)


def _branch_chunk(cases: list[BenchCase]) -> BranchData:
    return branch_data(cases)


def prepare(cfg: ZooConfig, jobs: int = 1) -> Shared:
    cases = cfg.train()
    t0 = time.perf_counter()
    parts = _pmap(_explore_chunk, [(c, cfg.seed) for c in _chunks(cases, jobs)], jobs)
    explore = [o for p in parts for o in p]
    explore_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    bparts: list[BranchData] = _pmap(_branch_chunk, [(c,) for c in _chunks(cases, jobs)], jobs)
    branches = BranchData()
    for b in bparts:
        branches.rows += b.rows
        branches.outcomes += b.outcomes
        branches.episodes += b.episodes
    branches.seconds = time.perf_counter() - t1
    return Shared(explore, explore_s, branches, fit_confidence(explore))


# --- per agent ----------------------------------------------------------------------


def _dev_score(
    agent: ZooAgent,
    claim: bool,
    conf: dict[str, float],
    dev: Sequence[BenchCase],
    objective: Objective,
) -> tuple[float, int, float]:
    t0 = time.perf_counter()
    outs = run_cases(ZooPolicy(agent, claim_win=claim, confidence=conf), dev)
    return mean_value(outs, objective, claim), len(outs), time.perf_counter() - t0


def _online(
    agent: LinUCBAgent | QAgent,
    cases: Sequence[BenchCase],
    objective: Objective,
    conf: dict[str, float],
) -> list[Outcome]:
    policy = ZooPolicy(agent, confidence=conf)
    outs = []
    for c in cases:
        o = run_case(policy, c)
        agent.learn(o, objective)
        outs.append(o)
    return outs


def train_one(name: str, objective: Objective, cfg: ZooConfig, shared: Shared) -> TrainedAgent:
    """Train and dev-tune one agent type for one objective."""
    dev = cfg.dev()
    conf = shared.confidence
    label = name if objective == "scorecard" else f"{name}_proxy"
    best: tuple[float, int] | None = None
    best_agent: ZooAgent | None = None
    best_claim = False
    best_cfg: dict[str, Any] = {}
    grid_out: list[dict[str, Any]] = []
    train_eps = 0
    train_s = 0.0
    dev_eps = 0
    dev_s = 0.0
    shared_tags: tuple[str, ...] = ()
    for i, params in enumerate(GRIDS[name]):
        t0 = time.perf_counter()
        agent: ZooAgent
        if name == "expert_rules":
            agent = ExpertAgent(escalate_min=int(params["escalate_min"]))
            claim = False  # hand-written: always honest
        elif name in ("logreg", "gbm"):
            model = fit_model(shared.branches, objective, name, **params)
            agent = ClassifierAgent(model, name=label)
            claim = claims_pay(shared.branches.outcomes, objective)
            if i == 0:
                train_eps += shared.branches.episodes
                train_s += shared.branches.seconds
            shared_tags = ("branches",)
        elif name == "bandit_linucb":
            learner = LinUCBAgent(alpha=float(params["alpha"]), name=label)
            outs = _online(learner, cfg.train(), objective, conf)
            train_eps += len(outs)
            agent = learner.frozen()
            claim = claims_pay(outs, objective)
        elif name == "q_learning":
            q = QAgent(epsilon=float(params["epsilon"]), seed=cfg.seed, name=label)
            outs = _online(q, cfg.train(), objective, conf)
            train_eps += len(outs)
            agent = q.frozen()
            claim = claims_pay(outs, objective)
        elif name == "mc_planner":
            pm = PlannerModel(objective, min_count=int(params["min_count"])).fit(shared.explore)
            agent = PlannerAgent(pm, rollouts=cfg.rollouts, seed=cfg.seed, name=label)
            claim = claims_pay(shared.explore, objective)
            if i == 0:
                train_eps += len(shared.explore)
                train_s += shared.explore_seconds
            shared_tags = ("explore",)
        else:
            raise ValueError(f"unknown agent type {name!r}")
        train_s += time.perf_counter() - t0
        v, n, s = _dev_score(agent, claim, conf, dev, objective)
        dev_eps += n
        dev_s += s
        grid_out.append({"config": params, "claim_win": claim, "dev_value": round(v, 4)})
        key = (v, -i)
        if best is None or key > best:
            best, best_agent, best_claim, best_cfg = key, agent, claim, params
    assert best is not None
    assert best_agent is not None
    return TrainedAgent(
        name=label,
        kind=KIND[name],
        objective=objective,
        policy=ZooPolicy(best_agent, claim_win=best_claim, confidence=conf, name=label),
        config=best_cfg,
        claim_win=best_claim,
        train_episodes=train_eps,
        train_seconds=train_s,
        dev_episodes=dev_eps,
        dev_seconds=dev_s,
        dev_value=best[0],
        dev_grid=grid_out,
        shared_data=shared_tags,
    )


def random_agent(cfg: ZooConfig, shared: Shared) -> TrainedAgent:
    v, n, s = _dev_score(
        RandomAgent(seed=cfg.seed), False, shared.confidence, cfg.dev(), "scorecard"
    )
    return TrainedAgent(
        name="random",
        kind="random",
        objective=None,
        policy=ZooPolicy(RandomAgent(seed=cfg.seed), confidence=shared.confidence),
        config={"seed": cfg.seed},
        claim_win=False,
        train_episodes=0,
        train_seconds=0.0,
        dev_episodes=n,
        dev_seconds=s,
        dev_value=v,
    )


def jobs_for(learners: Sequence[str], proxy: bool) -> list[tuple[str, Objective]]:
    out: list[tuple[str, Objective]] = [("expert_rules", "scorecard")]
    out += [(n, "scorecard") for n in learners]
    if proxy:
        out += [(n, "proxy") for n in learners]
    return out


def train_zoo(
    cfg: ZooConfig,
    *,
    jobs: int = 1,
    learners: Sequence[str] = LEARNERS,
    proxy: bool = True,
) -> tuple[dict[str, TrainedAgent], Shared]:
    shared = prepare(cfg, jobs)
    tasks = [(n, o, cfg, shared) for n, o in jobs_for(learners, proxy)]
    trained: list[TrainedAgent] = _pmap(train_one, tasks, jobs)
    out = {"random": random_agent(cfg, shared)}
    out |= {t.name: t for t in trained}
    return out, shared


__all__ = [
    "GRIDS",
    "KIND",
    "LEARNERS",
    "Shared",
    "TrainedAgent",
    "ZooConfig",
    "claims_pay",
    "fit_confidence",
    "jobs_for",
    "prepare",
    "random_agent",
    "train_one",
    "train_zoo",
]
