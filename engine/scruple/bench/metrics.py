"""Benchmark scores with confidence intervals (174).

Every interval is a 95% normal-approximation interval for a mean over cases,
`mean +- 1.96 * sd / sqrt(n)` (sample sd). At n = 10,000 the approximation is good
for the bounded quantities here (reward in [0, 1], rates, and the merchant's cost,
which is bounded by the disputed amount plus fees). It is a per-metric interval, not
a simultaneous one. Synthetic episodes only.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from scruple.bench.runner import BlockResult, CaseRow

Z95 = 1.959963984540054


class Estimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: float
    low: float
    high: float
    n: int

    def text(self, digits: int = 4) -> str:
        return f"{self.mean:.{digits}f} [{self.low:.{digits}f}, {self.high:.{digits}f}]"


def estimate(xs: Sequence[float], lo: float = -math.inf, hi: float = math.inf) -> Estimate:
    """Mean and 95% interval, clipped to the range the quantity lives in."""
    n = len(xs)
    if n == 0:
        return Estimate(mean=0.0, low=0.0, high=0.0, n=0)
    m = math.fsum(xs) / n
    if n < 2:
        return Estimate(mean=m, low=m, high=m, n=n)
    var = math.fsum((x - m) ** 2 for x in xs) / (n - 1)
    half = Z95 * math.sqrt(var / n)
    return Estimate(mean=m, low=max(lo, m - half), high=min(hi, m + half), n=n)


class GroupScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    reward: Estimate
    gate: Estimate
    cost: Estimate


class Score(BaseModel):
    """One agent on one benchmark (or a prefix of it)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    n_cases: int
    reward: Estimate
    gate: Estimate  # share of episodes passing every hard check
    quality: Estimate
    cost: Estimate  # merchant cost, USD minor units per case
    regret: Estimate  # cost minus the evidence baseline's cost on the same case
    correct: Estimate
    win: Estimate
    lapsed: int
    hard_failures: dict[str, int]
    batch_blocks: int
    batch_failed: dict[str, int]  # batch check id -> blocks failing it
    by_family: dict[str, GroupScore]
    by_tier: dict[str, GroupScore]
    synthetic: bool = True


def _unit(xs: Sequence[float]) -> Estimate:
    return estimate(xs, 0.0, 1.0)


def _group(rows: Sequence[CaseRow]) -> GroupScore:
    return GroupScore(
        n=len(rows),
        reward=_unit([r.reward for r in rows]),
        gate=_unit([float(r.gate) for r in rows]),
        cost=estimate([float(r.cost) for r in rows]),
    )


def _by(rows: Sequence[CaseRow], key: Callable[[CaseRow], str]) -> dict[str, GroupScore]:
    groups: dict[str, list[CaseRow]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return {k: _group(v) for k, v in sorted(groups.items())}


def rows_of(results: Sequence[BlockResult]) -> list[CaseRow]:
    return [r for b in results for r in b.rows]


def score(agent: str, results: Sequence[BlockResult]) -> Score:
    rows = rows_of(results)
    fails: Counter[str] = Counter(f for r in rows for f in r.hard_failures)
    batch_failed: Counter[str] = Counter(k for b in results for k, ok in b.batch.items() if not ok)
    return Score(
        agent=agent,
        n_cases=len(rows),
        reward=_unit([r.reward for r in rows]),
        gate=_unit([float(r.gate) for r in rows]),
        quality=_unit([r.quality for r in rows]),
        cost=estimate([float(r.cost) for r in rows]),
        regret=estimate(
            [float(r.cost - r.baseline_cost) for r in rows if r.baseline_cost is not None]
        ),
        correct=_unit([float(r.correct) for r in rows]),
        win=_unit([float(r.won) for r in rows]),
        lapsed=sum(r.lapsed for r in rows),
        hard_failures=dict(sorted(fails.items())),
        batch_blocks=len(results),
        batch_failed=dict(sorted(batch_failed.items())),
        by_family=_by(rows, lambda r: r.family),
        by_tier=_by(rows, lambda r: r.tier),
    )


def render(scores: Sequence[Score]) -> str:
    lines = [
        "| agent | cases | reward | gate pass | cost (USD minor) | regret vs evidence "
        "| correct | win | hard failures |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        fails = ", ".join(f"{k} {v}" for k, v in s.hard_failures.items()) or "-"
        lines.append(
            f"| {s.agent} | {s.n_cases} | {s.reward.text(3)} | {s.gate.text(3)} "
            f"| {s.cost.text(0)} | {s.regret.text(0)} | {s.correct.text(3)} "
            f"| {s.win.text(3)} | {fails} |"
        )
    return "\n".join(lines)


__all__ = ["Estimate", "GroupScore", "Score", "estimate", "render", "rows_of", "score"]
