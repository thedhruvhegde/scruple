"""A Monte Carlo planner with its own model of the issuer, fitted on train episodes.

The planner never reads ground truth and never touches the simulator's issuer or
its random stream. Its whole world is `PlannerModel`, fitted from outcomes the
merchant observes on train episodes played by an exploratory (random) behaviour
policy:

- issuer odds: `P(the merchant prevails | stage, evidence strength cited, style)`,
  Beta(1, 1)-smoothed counts of observed fights, backing off to the same strength
  at any stage (0.5 when even that is unseen) (a fight at inquiry is lost when the
  case comes back as a chargeback, a representment is lost when pre-arbitration
  follows, an arbitration by the final resolution). Strength is `moot`, `ce3`, or
  the coverage bucket of what was cited; style is `honest`, `flawed` (cited documents
  that fail the genuineness checks) or `fabricated` (uploaded the missing kinds);
- settle odds: how often a wait with a refund pending ended with a settled refund on
  file at the next decision;
- terminal values: the mean objective (scorecard reward or proxy) of train episodes by
  (category, coverage bucket, strength, style, how they ended), with back-off to
  coarser keys. This is where fairness enters: the planner learns that winning on
  partial evidence tends to score low under the scorecard, without seeing why.

Planning, at each decision point (receding horizon): every plan is a first act plus a
continuation rule for later stages (`fight` the same way again, or `concede`). Each
plan is scored by `rollouts` Monte Carlo simulations of the lifecycle under the model
(inquiry -> chargeback -> pre-arbitration -> arbitration), drawn from a
`random.Random` seeded by the episode id and step. `WAIT` is scored as the
settle-odds mixture of the best non-waiting plan with and without a settled refund.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from scruple.zoo.context import Act, DecisionContext, coverage_bucket
from scruple.zoo.rollout import Objective, Outcome

NEXT_STAGE = {"inquiry": "chargeback", "chargeback": "pre_arbitration"}
FIGHT_STYLE = {
    Act.REPRESENT: "honest",
    Act.ESCALATE: "honest",
    Act.CITE_FLAWED: "flawed",
    Act.FABRICATE: "fabricated",
}
STYLE_RANK = {"honest": 0, "flawed": 1, "fabricated": 2}


def strength(ctx: DecisionContext, act: Act) -> str:
    if act is Act.FABRICATE:
        return "full"
    if ctx.moot_docs():
        return "moot"
    if ctx.ce3_docs():
        return "ce3"
    if act is Act.CITE_FLAWED:
        kinds = {d["kind"] for d in ctx.docs if d["doc_id"] in set(ctx.flawed_ids())}
        req = list(ctx.case["required_evidence"])
        return coverage_bucket(sum(1 for k in req if k in kinds) / len(req) if req else 1.0)
    return coverage_bucket(ctx.coverage())


def episode_style(acts: Sequence[Act]) -> str:
    styles = [FIGHT_STYLE[a] for a in acts if a in FIGHT_STYLE]
    return max(styles, key=lambda s: STYLE_RANK[s]) if styles else "honest"


def fight_results(out: Outcome) -> list[tuple[str, str, str, bool]]:
    """(stage, strength, style, merchant prevailed) for every fight in an episode,
    from what the merchant observes: the next decision's stage, or the resolution."""
    res = []
    n = len(out.acts)
    for k, (ctx, a) in enumerate(zip(out.contexts, out.acts, strict=True)):
        if a not in FIGHT_STYLE:
            continue
        stage = ctx.stage
        later = [c.stage for c in out.contexts[k + 1 :] if c.stage != stage]
        if stage in NEXT_STAGE and later:
            won = later[0] != NEXT_STAGE[stage]
        elif k < n - 1 and stage in NEXT_STAGE and not later:
            continue  # the next decision is the same stage (a refused act): unknown
        else:
            won = out.won
        res.append((stage, strength(ctx, a), FIGHT_STYLE[a], won))
    return res


def terminal_class(out: Outcome) -> str:
    """How the episode ended, from the merchant's side: `<how>@<stage>`."""
    for ctx, a in zip(reversed(out.contexts), reversed(out.acts), strict=True):
        if a is Act.WAIT:
            continue
        if a is Act.CONCEDE:
            return f"conceded@{ctx.stage}"
        return f"{'won' if out.won else 'lost'}@{ctx.stage}"
    return "idle"


def win_keys(stage: str, strong: str, style: str) -> list[tuple[str, ...]]:
    """Issuer-odds keys, most specific first; back-off keeps the evidence strength."""
    return [(stage, strong, style), (stage, strong), (strong, style), (strong,)]


def value_keys(category: str, cov: str, strong: str, style: str, end: str) -> list[tuple[str, ...]]:
    return [
        (category, cov, strong, style, end),
        (cov, strong, style, end),
        (style, end),
        (end,),
        (),
    ]


def _last_decision(out: Outcome) -> DecisionContext | None:
    return out.contexts[-1] if out.contexts else None


@dataclass
class PlannerModel:
    objective: Objective
    prior: float = 1.0
    wins: dict[tuple[str, ...], list[float]] = field(default_factory=dict)  # key -> [won, n]
    values: dict[tuple[str, ...], list[float]] = field(default_factory=dict)  # key -> [sum, n]
    settle: list[float] = field(default_factory=lambda: [0.0, 0.0])
    min_count: int = 5
    episodes: int = 0

    def fit(self, outs: Sequence[Outcome]) -> PlannerModel:
        for out in outs:
            self.episodes += 1
            for stage, strong, style, won in fight_results(out):
                for key in win_keys(stage, strong, style):
                    w = self.wins.setdefault(key, [0.0, 0.0])
                    w[0] += float(won)
                    w[1] += 1.0
            for k, (ctx, a) in enumerate(zip(out.contexts, out.acts, strict=True)):
                if a is Act.WAIT and ctx.refund_pending() and k + 1 < len(out.contexts):
                    self.settle[0] += float(bool(out.contexts[k + 1].moot_docs()))
                    self.settle[1] += 1.0
            last = _last_decision(out)
            if last is None:
                continue
            strong = "moot" if last.moot_docs() else ("ce3" if last.ce3_docs() else "-")
            keys = value_keys(
                str(last.case["category"]),
                coverage_bucket(last.coverage()),
                strong,
                episode_style(out.acts),
                terminal_class(out),
            )
            v = out.value(self.objective)
            for vkey in keys:
                s = self.values.setdefault(vkey, [0.0, 0.0])
                s[0] += v
                s[1] += 1.0
        return self

    def p_win(self, stage: str, strong: str, style: str) -> float:
        for key in win_keys(stage, strong, style):
            w = self.wins.get(key)
            if w is not None and w[1] >= self.min_count:
                return (w[0] + self.prior) / (w[1] + 2 * self.prior)
        return 0.5

    def p_settle(self) -> float:
        won, n = self.settle
        return (won + self.prior) / (n + 2 * self.prior)

    def value(self, category: str, cov: str, strong: str, style: str, end: str) -> float:
        for key in value_keys(category, cov, strong, style, end):
            s = self.values.get(key)
            if s is not None and s[1] >= self.min_count:
                return s[0] / s[1]
        s = self.values.get((), [0.0, 1.0])
        return s[0] / max(s[1], 1.0)


@dataclass
class PlannerAgent:
    model: PlannerModel
    rollouts: int = 32
    seed: int = 0
    name: str = "mc_planner"
    kind: str = "planner"

    def _rollout(
        self, ctx: DecisionContext, first: Act, cont: str, rng: random.Random, moot: bool
    ) -> float:
        m = self.model
        cat = str(ctx.case["category"])
        cov = coverage_bucket(ctx.coverage())
        strong0 = "moot" if moot else ("ce3" if ctx.ce3_docs() else "-")
        style = FIGHT_STYLE.get(first, "honest")
        strong = "moot" if moot else strength(ctx, first)
        stage = ctx.stage
        act = first
        while True:
            if act is Act.CONCEDE:
                return m.value(cat, cov, strong0, style, f"conceded@{stage}")
            if rng.random() < m.p_win(stage, strong, style):
                return m.value(cat, cov, strong0, style, f"won@{stage}")
            if act is Act.ESCALATE or stage not in NEXT_STAGE:
                return m.value(cat, cov, strong0, style, f"lost@{stage}")
            stage = NEXT_STAGE[stage]
            if cont == "concede":
                act = Act.CONCEDE
            elif stage == "pre_arbitration":
                act = Act.ESCALATE
            else:
                act = first

    def _plan_value(
        self, ctx: DecisionContext, first: Act, rng: random.Random, moot: bool
    ) -> float:
        conts = ("concede",) if first is Act.CONCEDE else ("fight", "concede")
        best = -1.0
        for cont in conts:
            v = sum(self._rollout(ctx, first, cont, rng, moot) for _ in range(self.rollouts))
            best = max(best, v / self.rollouts)
        return best

    def plan(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> dict[Act, float]:
        rng = random.Random(f"zoo-planner:{self.seed}:{ctx.episode_id}:{ctx.step}")
        moot = bool(ctx.moot_docs())
        out: dict[Act, float] = {}
        now = [a for a in legal if a is not Act.WAIT]
        for a in now:
            out[a] = self._plan_value(ctx, a, rng, moot)
        if Act.WAIT in legal:
            p = self.model.p_settle() if ctx.refund_pending() else 0.0
            stay = max(out.values(), default=0.0)
            fights = [a for a in now if a in (Act.REPRESENT, Act.ESCALATE)] or [Act.REPRESENT]
            settled = max(self._plan_value(ctx, a, rng, True) for a in fights)
            out[Act.WAIT] = p * max(settled, stay) + (1 - p) * stay - 1e-6  # ties: act now
        return out

    def act(self, ctx: DecisionContext, legal: tuple[Act, ...]) -> Act:
        vals = self.plan(ctx, legal)
        best = max(vals.values())
        order = (Act.CONCEDE, Act.REPRESENT, Act.ESCALATE, Act.WAIT, Act.CITE_FLAWED, Act.FABRICATE)
        return next(a for a in order if a in vals and vals[a] >= best - 1e-12)


__all__ = [
    "PlannerAgent",
    "PlannerModel",
    "episode_style",
    "fight_results",
    "strength",
    "terminal_class",
]
