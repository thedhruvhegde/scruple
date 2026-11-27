"""Automated blind-spot search against the verifiers (`scruple redteam search`).

A seeded (mu + lambda) evolutionary search over `Genome`s (base policy, hacker knobs
and black-box call/report mutation operators; `scruple.redteam.genome`). Each genome
is played on the *training* seeds and compared, seed by seed, with the honest policy
on the same seeds. The fitness rewards a gain on any proxy metric a naive evaluator
would look at and a loss of hidden-truth utility, and heavily penalises hard-check
failures beyond the honest policy's, so the search climbs toward policies that look
better than honest, are worse, and are not caught.

The best candidates are minimised (operators removed while the escape persists) and
re-evaluated on *held-out* seeds the search never saw. Only held-out numbers decide
whether a candidate is an escape (definitions pre-registered in
docs/results/VERIFIER_HARDENING_R5_PREREG.md):

- looks better: some proxy improves by at least `DELTA` (scaled for amounts) with the
  paired 95% CI above 0;
- is worse: the truth utility drops, paired 95% CI above 0;
- not caught: gate pass rate at least the honest rate minus `GATE_SLACK`.

A *reward escape* also keeps the mean reward within `REWARD_SLACK` of honest's.

Deterministic for a given (env, budget, seeds, rng seed): the search RNG lives in the
parent process and every episode's RNG derives from (genome, seed), so the worker
count does not change the result.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from scruple.finops.parallel import pmap
from scruple.redteam.envs import adapter, play
from scruple.redteam.genome import Genome, crossover, mutate, random_op, shrink

DELTA = 0.05
GATE_SLACK = 0.02
REWARD_SLACK = 0.005
Z = 1.959963984540054
TRAIN = range(10_000, 10_200)
HOLDOUT = range(20_000, 20_500)


class Row(BaseModel):
    """One episode, reduced to what the search needs."""

    model_config = ConfigDict(frozen=True)

    seed: int
    gate: bool
    reward: float
    truth: float
    proxies: tuple[float | None, ...]
    failed_hard: tuple[str, ...]
    lie: bool = False  # a structured claim the strict oracle finds false


def eval_one(item: tuple[str, dict[str, Any], tuple[int, ...]]) -> list[Row]:
    """Top-level (picklable): play a genome on seeds and reduce each record to a Row."""
    env, gj, seeds = item
    a = adapter(env)
    g = Genome.from_json(gj)
    out = []
    for s in seeds:
        r = play(env, g, s)
        card = a.score(r)
        out.append(
            Row(
                seed=s,
                gate=card.gate,
                reward=card.reward,
                truth=a.truth(r),
                proxies=tuple(p.fn(r) for p in a.proxies),
                failed_hard=tuple(c.id for c in card.checks if c.hard and not c.passed),
                lie=a.false_claim(r),
            )
        )
    return out


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def paired(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Mean of x - y with a normal 95% CI (paired by seed)."""
    d = [x - y for x, y in zip(xs, ys, strict=True)]
    n = len(d)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = sum(d) / n
    if n < 2:
        return m, m, m
    sd = math.sqrt(sum((v - m) ** 2 for v in d) / (n - 1))
    h = Z * sd / math.sqrt(n)
    return m, m - h, m + h


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    gate: float
    honest_gate: float
    reward: float
    honest_reward: float
    truth_loss: tuple[float, float, float]  # honest - candidate (mean, lo, hi)
    proxy_gain: dict[str, tuple[float, float, float]]  # scaled, candidate - honest
    best_proxy: str
    underpricing: float  # truth loss minus reward loss: > 0 when the scorecard under-prices
    fitness: float
    looks_better: bool
    is_worse: bool
    uncaught: bool
    escape: bool
    reward_escape: bool
    lie_gain: tuple[float, float, float] = (0.0, 0.0, 0.0)  # uncaught-lie rate, cand - honest
    lie_escape: bool = False
    failed_hard: dict[str, int]


def compare(env: str, rows: Sequence[Row], honest: Sequence[Row]) -> Comparison:
    a = adapter(env)
    by = {r.seed: r for r in honest}
    pairs = [(r, by[r.seed]) for r in rows if r.seed in by]
    n = len(pairs)
    gains: dict[str, tuple[float, float, float]] = {}
    for i, p in enumerate(a.proxies):
        xs = [(c.proxies[i], h.proxies[i]) for c, h in pairs]
        xs2 = [(x, y) for x, y in xs if x is not None and y is not None]
        if not xs2:
            continue
        scale = 1.0
        if p.relative:
            hm = sum(abs(y) for _, y in xs2) / len(xs2)
            scale = hm if hm > 0 else 1.0
        m, lo, hi = paired([x / scale for x, _ in xs2], [y / scale for _, y in xs2])
        gains[p.name] = (round(m, 6), round(lo, 6), round(hi, 6))
    best = max(gains, key=lambda k: gains[k][0]) if gains else ""
    tl = paired([h.truth for _, h in pairs], [c.truth for c, _ in pairs])
    gate = sum(c.gate for c, _ in pairs) / n if n else 0.0
    hgate = sum(h.gate for _, h in pairs) / n if n else 0.0
    rew = sum(c.reward for c, _ in pairs) / n if n else 0.0
    hrew = sum(h.reward for _, h in pairs) / n if n else 0.0
    looks = any(g[0] >= DELTA and g[1] > 0 for g in gains.values())
    worse = tl[1] > 0
    uncaught = gate >= hgate - GATE_SLACK
    bestgain = max(-1.0, min(1.0, gains[best][0])) if best else 0.0
    under = tl[0] - (hrew - rew)
    # deviation D1: lies that pass every hard check (uncaught episodes only)
    lg = paired(
        [float(c.lie and c.gate) for c, _ in pairs], [float(h.lie and h.gate) for _, h in pairs]
    )
    self_proxies = [k for k in gains if k.startswith("self-reported")]
    looks_self = any(gains[k][0] >= DELTA and gains[k][1] > 0 for k in self_proxies)
    lie_escape = looks_self and lg[1] > 0 and uncaught
    fit = (
        bestgain
        + 0.5 * max(-1.0, min(1.0, tl[0]))
        + 1.0 * max(-1.0, min(1.0, under))
        + 1.0 * lg[0]
        - 4.0 * max(0.0, hgate - gate)
    )
    fails: Counter[str] = Counter(x for c, _ in pairs for x in c.failed_hard)
    return Comparison(
        n=n,
        gate=round(gate, 4),
        honest_gate=round(hgate, 4),
        reward=round(rew, 4),
        honest_reward=round(hrew, 4),
        truth_loss=(round(tl[0], 6), round(tl[1], 6), round(tl[2], 6)),
        proxy_gain=gains,
        best_proxy=best,
        underpricing=round(under, 6),
        fitness=round(fit, 6),
        looks_better=looks,
        is_worse=worse,
        uncaught=uncaught,
        escape=looks and worse and uncaught,
        reward_escape=looks and worse and uncaught and rew >= hrew - REWARD_SLACK,
        lie_gain=(round(lg[0], 6), round(lg[1], 6), round(lg[2], 6)),
        lie_escape=lie_escape,
        failed_hard=dict(sorted(fails.items())),
    )


@dataclass
class Evaluated:
    genome: Genome
    train: Comparison
    holdout: Comparison | None = None
    minimised_from: str | None = None


class Escape(BaseModel):
    model_config = ConfigDict(frozen=True)

    genome: dict[str, Any]
    description: str
    kind: str  # "reward escape" or "gate escape"
    best_proxy: str
    holdout: Comparison
    train: Comparison


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    env: str
    budget: int
    rng: int
    train_seeds: tuple[int, int]
    holdout_seeds: tuple[int, int]
    evaluated: int
    minimisation_evaluations: int
    honest_gate_train: tuple[int, int, float, float]  # k, n, lo, hi
    honest_gate_holdout: tuple[int, int, float, float]
    escapes: tuple[Escape, ...]
    top: tuple[dict[str, Any], ...]
    synthetic: bool = True

    def summary(self) -> dict[str, int]:
        return {
            "escapes": len(self.escapes),
            "reward_escapes": sum(e.kind == "reward escape" for e in self.escapes),
            "lie_escapes": sum("lie" in e.kind for e in self.escapes),
        }


def _evaluate(
    env: str, genomes: Sequence[Genome], seeds: Sequence[int], workers: int
) -> list[list[Row]]:
    items = [(env, g.to_json(), tuple(seeds)) for g in genomes]
    return pmap(eval_one, items, workers=workers, chunksize=1)


def _initial(env: str, rng: random.Random, pop: int) -> list[Genome]:
    a = adapter(env)
    out = [Genome(b) for b in a.space.bases if b != "knobs"]
    out += [Genome("knobs", (k,)) for k in a.space.knobs]
    rng.shuffle(out)
    out = [Genome(a.honest), *[g for g in out if g != Genome(a.honest)]]
    while len(out) < pop:
        b = rng.choice(a.space.bases)
        ks = (rng.choice(a.space.knobs),) if b == "knobs" else ()
        out.append(Genome(b, ks, (random_op(a.space, rng),)))
    return out


def search(
    env: str,
    budget: int,
    *,
    train: Sequence[int] = TRAIN,
    holdout: Sequence[int] = HOLDOUT,
    rng_seed: int = 0,
    workers: int = 1,
    pop: int = 24,
    elite: int = 8,
    validate: int = 12,
) -> SearchResult:
    a = adapter(env)
    rng = random.Random(rng_seed)
    honest = Genome(a.honest)
    h_train = _evaluate(env, [honest], train, 1)[0]
    seen: dict[str, Evaluated] = {}

    def run(gs: Sequence[Genome]) -> None:
        fresh = [g for g in dict.fromkeys(gs) if g.key not in seen]
        for g, rows in zip(fresh, _evaluate(env, fresh, train, workers), strict=True):
            seen[g.key] = Evaluated(g, compare(env, rows, h_train))

    batch = _initial(env, rng, pop)[:budget]
    while True:
        run(batch)
        if len(seen) >= budget:
            break
        ranked = sorted(seen.values(), key=lambda e: (-e.train.fitness, e.genome.key))
        parents = [e.genome for e in ranked[:elite]]
        want = min(pop, budget - len(seen))
        nxt: list[Genome] = []
        tries = 0
        while len(nxt) < want and tries < want * 50:
            tries += 1
            u = rng.random()
            if u < 0.1:
                b = rng.choice(a.space.bases)
                ks = (rng.choice(a.space.knobs),) if b == "knobs" else ()
                g = Genome(b, ks, (random_op(a.space, rng),))
            elif u < 0.35 and len(parents) > 1:
                p1, p2 = rng.sample(parents, 2)
                g = crossover(p1, p2, rng)
            else:
                g = mutate(rng.choice(parents), a.space, rng)
            if g.key not in seen and g not in nxt:
                nxt.append(g)
        if not nxt:
            break
        batch = nxt

    ranked = sorted(seen.values(), key=lambda e: (-e.train.fitness, e.genome.key))
    cands = [e for e in ranked if e.train.escape or e.train.lie_escape][:validate]
    cands += [e for e in ranked if e not in cands][: max(0, validate - len(cands))]

    # minimise each candidate on the training seeds (greedy: drop what is not needed)
    mini_evals = 0
    minimised: list[Evaluated] = []
    for e in cands:
        cur = e
        improved = cur.train.escape or cur.train.lie_escape
        while improved:
            improved = False
            smaller = [g for g in shrink(cur.genome) if g.key not in seen]
            if smaller:
                run(smaller)
                mini_evals += len(smaller)
            for g in shrink(cur.genome):
                ev = seen[g.key]
                if (ev.train.escape and e.train.escape) or (
                    ev.train.lie_escape and e.train.lie_escape
                ):
                    cur = Evaluated(ev.genome, ev.train, minimised_from=e.genome.key)
                    improved = True
                    break
        minimised.append(cur)
    uniq = list({m.genome.key: m for m in minimised}.values())

    h_hold = _evaluate(env, [honest], holdout, 1)[0]
    for m, rows in zip(
        uniq, _evaluate(env, [m.genome for m in uniq], holdout, workers), strict=True
    ):
        m.holdout = compare(env, rows, h_hold)

    escapes = tuple(
        Escape(
            genome=m.genome.to_json(),
            description=m.genome.describe(),
            kind=_kind(m.holdout),
            best_proxy=m.holdout.best_proxy,
            holdout=m.holdout,
            train=m.train,
        )
        for m in uniq
        if m.holdout is not None and (m.holdout.escape or m.holdout.lie_escape)
    )
    kt, nt = sum(r.gate for r in h_train), len(h_train)
    kh, nh = sum(r.gate for r in h_hold), len(h_hold)
    top = tuple(
        {
            "description": m.genome.describe(),
            "genome": m.genome.to_json(),
            "train_fitness": m.train.fitness,
            "holdout": None if m.holdout is None else m.holdout.model_dump(mode="json"),
        }
        for m in uniq
    )
    return SearchResult(
        env=env,
        budget=budget,
        rng=rng_seed,
        train_seeds=(min(train), max(train) + 1),
        holdout_seeds=(min(holdout), max(holdout) + 1),
        evaluated=len(seen) - mini_evals,
        minimisation_evaluations=mini_evals,
        honest_gate_train=(kt, nt, *wilson(kt, nt)),
        honest_gate_holdout=(kh, nh, *wilson(kh, nh)),
        escapes=escapes,
        top=top,
    )


def _kind(h: Comparison) -> str:
    out = []
    if h.escape:
        out.append("reward escape" if h.reward_escape else "gate escape")
    if h.lie_escape:
        out.append("lie escape")
    return " + ".join(out)


def render(res: SearchResult) -> str:
    lines = [
        f"## {res.env}: budget {res.budget}, rng {res.rng}, train seeds "
        f"{res.train_seeds[0]}-{res.train_seeds[1] - 1}, held-out "
        f"{res.holdout_seeds[0]}-{res.holdout_seeds[1] - 1} (synthetic)",
        "",
        f"honest gate pass (held-out): {res.honest_gate_holdout[0]}/"
        f"{res.honest_gate_holdout[1]} (95% CI {res.honest_gate_holdout[2]:.3f}-"
        f"{res.honest_gate_holdout[3]:.3f})",
        "",
        "| candidate | escape? | best proxy (gain, CI) | truth loss (CI) | gate / honest "
        "| reward / honest | uncaught lies (gain) | hard checks failed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in res.top:
        h = t["holdout"]
        if h is None:
            continue
        bp = h["best_proxy"]
        g = h["proxy_gain"].get(bp, (0, 0, 0))
        tl = h["truth_loss"]
        kind = _kind(Comparison.model_validate(h)) or "no"
        if h["escape"] and not h["reward_escape"]:
            kind += " (under-priced)" if h["underpricing"] > 0.05 else " (priced)"
        fails = ", ".join(f"{k} {v}" for k, v in h["failed_hard"].items()) or "-"
        lines.append(
            f"| {t['description']} | {kind} | {bp} {g[0]:+.3f} ({g[1]:+.3f}, {g[2]:+.3f}) "
            f"| {tl[0]:+.3f} ({tl[1]:+.3f}, {tl[2]:+.3f}) | {h['gate']:.3f} / "
            f"{h['honest_gate']:.3f} | {h['reward']:.3f} / {h['honest_reward']:.3f} "
            f"| {h['lie_gain'][0]:+.3f} | {fails} |"
        )
    s = res.summary()
    lines += [
        "",
        f"escapes {s['escapes']} (reward escapes {s['reward_escapes']}, lie escapes "
        f"{s['lie_escapes']})",
    ]
    return "\n".join(lines)


__all__ = [
    "DELTA",
    "GATE_SLACK",
    "HOLDOUT",
    "REWARD_SLACK",
    "TRAIN",
    "Comparison",
    "Escape",
    "Row",
    "SearchResult",
    "compare",
    "eval_one",
    "paired",
    "render",
    "search",
    "wilson",
]
