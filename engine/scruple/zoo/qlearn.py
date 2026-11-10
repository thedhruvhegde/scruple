"""Tabular Q-learning over the dispute lifecycle.

State: `observable_key(ctx)`: stage, days left (short, mid, long), required-evidence
coverage (none, part, full), moot or CE 3.0 evidence on file, a refund pending, and
the reason-code category. Actions: every macro-act, `WAIT` included (it moves the
state: fewer days left, maybe a settled refund on file). Reward: zero until the
episode closes, then the episode's objective value. Undiscounted (an episode has at
most a handful of decisions).

After each train episode the decisions are backed up last to first:

    target_t = R                                   for the last decision
             = max_{a' legal at t+1} Q(s_{t+1}, a')  otherwise
    Q(s_t, a_t) += lr_t * (target_t - Q(s_t, a_t)),  lr_t = max(lr_min, 1 / n(s_t, a_t))

Behaviour while training: epsilon-greedy with a seeded `random.Random`. Evaluation
is greedy; unvisited pairs score `q_init`, and ties go to the first of concede,
represent, escalate, wait, cite_flawed, fabricate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from scruple.zoo.context import Act, DecisionContext, legal_acts, observable_key
from scruple.zoo.rollout import Objective, Outcome

TIE_ORDER: tuple[Act, ...] = (
    Act.CONCEDE,
    Act.REPRESENT,
    Act.ESCALATE,
    Act.WAIT,
    Act.CITE_FLAWED,
    Act.FABRICATE,
)
State = tuple[str, ...]


@dataclass
class QAgent:
    epsilon: float = 0.2
    lr_min: float = 0.05
    q_init: float = 0.0
    seed: int = 0
    name: str = "q_learning"
    kind: str = "q_learning"
    explore: bool = True
    q: dict[tuple[State, Act], float] = field(default_factory=dict)
    n: dict[tuple[State, Act], int] = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self.rng = random.Random(f"zoo-q:{self.seed}")

    def value(self, s: State, a: Act) -> float:
        return self.q.get((s, a), self.q_init)

    def greedy(self, s: State, legal: tuple[Act, ...]) -> Act:
        best = max(self.value(s, a) for a in legal)
        return next(a for a in TIE_ORDER if a in legal and self.value(s, a) >= best - 1e-12)

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        if self.explore and self.rng.random() < self.epsilon:
            return self.rng.choice(legal)
        return self.greedy(observable_key(ctx), legal)

    def learn(self, out: Outcome, objective: Objective) -> None:
        r = out.value(objective)
        states = [observable_key(c) for c in out.contexts]
        for t in range(len(states) - 1, -1, -1):
            if t == len(states) - 1:
                target = r
            else:
                nxt = legal_acts(out.contexts[t + 1])
                target = max(self.value(states[t + 1], a) for a in nxt)
            key = (states[t], out.acts[t])
            self.n[key] = self.n.get(key, 0) + 1
            lr = max(self.lr_min, 1.0 / self.n[key])
            self.q[key] = self.value(*key) + lr * (target - self.value(*key))

    def frozen(self) -> QAgent:
        out = QAgent(
            epsilon=self.epsilon,
            lr_min=self.lr_min,
            q_init=self.q_init,
            seed=self.seed,
            name=self.name,
            explore=False,
            q=dict(self.q),
            n=dict(self.n),
        )
        return out

    @property
    def states(self) -> int:
        return len({s for s, _ in self.q})


__all__ = ["TIE_ORDER", "QAgent"]
