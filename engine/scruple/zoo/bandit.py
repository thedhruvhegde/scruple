"""A contextual bandit: disjoint LinUCB over the macro-acts (Li et al., 2010).

Each act `a` keeps a ridge regression of the episode's reward on the observable
feature vector (`features(ctx)` plus a bias, which includes the reason-code category
one-hots, so the arms are effectively per category). At a decision point it picks

    argmax_a  theta_a . x + alpha * sqrt(x' A_a^-1 x)          (training)
    argmax_a  theta_a . x                                     (evaluation)

over the legal acts, ties broken in `ACTS` order. It learns online, one train
episode at a time: every decision in the episode is credited with the episode's
reward (most episodes have one or two decisions). `A_a^-1` is kept with
Sherman-Morrison updates. Deterministic: no random draws at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from scruple.zoo.context import ACTS, N_FEATURES, Act, DecisionContext, features
from scruple.zoo.rollout import Objective, Outcome

DIM = N_FEATURES + 1


def phi(ctx: DecisionContext) -> Any:
    return np.asarray((*features(ctx), 1.0), dtype=float)


@dataclass
class LinUCBAgent:
    alpha: float = 0.5
    ridge: float = 1.0
    name: str = "bandit_linucb"
    kind: str = "bandit"
    explore: bool = True
    a_inv: dict[Act, Any] = field(default_factory=dict)
    b: dict[Act, Any] = field(default_factory=dict)
    updates: int = 0

    def __post_init__(self) -> None:
        for a in ACTS:
            self.a_inv.setdefault(a, np.eye(DIM) / self.ridge)
            self.b.setdefault(a, np.zeros(DIM))

    def scores(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> dict[Act, float]:
        x = phi(ctx)
        out: dict[Act, float] = {}
        for a in legal:
            ai = self.a_inv[a]
            mean = float(ai @ self.b[a] @ x)
            bonus = self.alpha * float(np.sqrt(max(0.0, x @ ai @ x))) if self.explore else 0.0
            out[a] = mean + bonus
        return out

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        s = self.scores(ctx, legal)
        best = max(s.values())
        return next(a for a in ACTS if a in s and s[a] >= best - 1e-12)

    def update(self, x: Any, a: Act, r: float) -> None:
        ai = self.a_inv[a]
        u = ai @ x
        self.a_inv[a] = ai - np.outer(u, u) / (1.0 + float(x @ u))
        self.b[a] = self.b[a] + r * x
        self.updates += 1

    def learn(self, out: Outcome, objective: Objective) -> None:
        r = out.value(objective)
        for ctx, a in zip(out.contexts, out.acts, strict=True):
            self.update(phi(ctx), a, r)

    def frozen(self) -> LinUCBAgent:
        """A greedy copy for evaluation."""
        return LinUCBAgent(
            alpha=self.alpha,
            ridge=self.ridge,
            name=self.name,
            explore=False,
            a_inv={a: v.copy() for a, v in self.a_inv.items()},
            b={a: v.copy() for a, v in self.b.items()},
            updates=self.updates,
        )


__all__ = ["DIM", "LinUCBAgent", "phi"]
