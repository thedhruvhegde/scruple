"""Supervised classifiers that decide fight or concede from observable features.

Data (train namespace only): for each train case the expert rule set plays the
episode; at every decision point where both a fight (represent, or escalate at
pre-arbitration) and a concession are open, the same episode is replayed with the
other choice forced at that point and the expert everywhere else. The simulator is
deterministic per seed, so the two branches share their prefix exactly. The label
is which branch scored higher under the learner's objective; the sample weight is
the size of the difference; ties carry no information and are dropped.

Features are `scruple.zoo.context.features(ctx)`: tool outputs only. The label uses
the episode's reward, which the verifiers compute with ground truth; that is the
training signal, not an input, and nothing truth-derived is in `x`.

Models: scikit-learn `LogisticRegression` and `GradientBoostingClassifier`, both with
fixed `random_state`. scikit-learn is imported lazily (it is a dev dependency).
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from scruple.bench.spec import BenchCase
from scruple.zoo.context import Act, DecisionContext, features, first_honest, legal_acts
from scruple.zoo.protocol import ZooAgent, ZooPolicy
from scruple.zoo.rollout import Objective, Outcome, run_case
from scruple.zoo.rules import ExpertAgent

FIGHTS = (Act.REPRESENT, Act.ESCALATE)


def fight_of(legal: Sequence[Act]) -> Act | None:
    return next((a for a in FIGHTS if a in legal), None)


@dataclass(frozen=True)
class Forced:
    """`base` everywhere except the decision indices in `plan` (when legal there)."""

    base: ZooAgent
    plan: tuple[tuple[int, Act], ...]
    name: str = "forced"
    kind: str = "forced"

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        for step, a in self.plan:
            if step == ctx.step and a in legal:
                return a
        return self.base.act(ctx, legal)


@dataclass
class BranchRow:
    x: tuple[float, ...]
    fight: dict[str, float]  # objective -> value with the fight branch
    concede: dict[str, float]


@dataclass
class BranchData:
    rows: list[BranchRow] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)  # every episode played
    episodes: int = 0
    seconds: float = 0.0


def _values(o: Outcome) -> dict[str, float]:
    return {"scorecard": o.reward, "proxy": o.proxy}


def branch_data(cases: Sequence[BenchCase], behaviour: ZooAgent | None = None) -> BranchData:
    """Paired branch rollouts on train cases (see the module docstring)."""
    base = behaviour or ExpertAgent()
    t0 = time.perf_counter()
    data = BranchData()
    for case in cases:
        main = run_case(ZooPolicy(base), case)
        data.outcomes.append(main)
        data.episodes += 1
        for k, (ctx, act) in enumerate(zip(main.contexts, main.acts, strict=True)):
            if act is Act.WAIT:
                continue
            legal = legal_acts(ctx)
            fight = fight_of(legal)
            if fight is None or Act.CONCEDE not in legal:
                continue
            other = Act.CONCEDE if act is fight else fight
            prefix = tuple((j, a) for j, a in enumerate(main.acts[:k]))
            alt = run_case(ZooPolicy(Forced(base, (*prefix, (k, other)))), case)
            data.outcomes.append(alt)
            data.episodes += 1
            if act is fight:
                f, c = main, alt
            elif act is Act.CONCEDE:
                f, c = alt, main
            else:  # pragma: no cover - the expert only fights, concedes or waits
                continue
            data.rows.append(BranchRow(features(ctx), _values(f), _values(c)))
    data.seconds = time.perf_counter() - t0
    return data


def xyw(data: BranchData, objective: Objective) -> tuple[Any, Any, Any]:
    xs, ys, ws = [], [], []
    for r in data.rows:
        d = r.fight[objective] - r.concede[objective]
        if abs(d) < 1e-12:
            continue
        xs.append(r.x)
        ys.append(int(d > 0))
        ws.append(abs(d))
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=int), np.asarray(ws, dtype=float)


def fit_model(data: BranchData, objective: Objective, model: str, **params: Any) -> Any:
    ensemble: Any = importlib.import_module("sklearn.ensemble")
    linear: Any = importlib.import_module("sklearn.linear_model")
    x, y, w = xyw(data, objective)
    if len(set(y.tolist())) < 2:
        return _Constant(float(y[0]) if len(y) else 0.0)
    if model == "logreg":
        clf: Any = linear.LogisticRegression(C=params.get("C", 1.0), max_iter=2_000, random_state=0)
    elif model == "gbm":
        clf = ensemble.GradientBoostingClassifier(
            n_estimators=params.get("n_estimators", 100),
            max_depth=params.get("max_depth", 2),
            learning_rate=params.get("learning_rate", 0.1),
            random_state=0,
        )
    else:
        raise ValueError(f"unknown model {model!r}")
    clf.fit(x, y, sample_weight=w)
    return clf


@dataclass(frozen=True)
class _Constant:
    p: float

    def predict_proba(self, x: Any) -> Any:
        n = len(x)
        return np.column_stack([np.full(n, 1 - self.p), np.full(n, self.p)])


@dataclass
class ClassifierAgent:
    """Fight when the model says the fight branch scores higher, else concede."""

    model: Any
    name: str
    kind: str = "supervised"
    threshold: float = 0.5

    def p_fight(self, ctx: DecisionContext) -> float:
        p = self.model.predict_proba(np.asarray([features(ctx)], dtype=float))[0, 1]
        return float(p)

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        fight = fight_of(legal)
        if fight is None:
            return first_honest(legal)
        if Act.CONCEDE not in legal:
            return fight
        return fight if self.p_fight(ctx) >= self.threshold else Act.CONCEDE


__all__ = [
    "FIGHTS",
    "BranchData",
    "BranchRow",
    "ClassifierAgent",
    "Forced",
    "branch_data",
    "fight_of",
    "fit_model",
    "xyw",
]
