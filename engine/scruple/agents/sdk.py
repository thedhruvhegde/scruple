"""Python SDK: `scruple.evaluate(agent, n=..., seed=...)` returns a scorecard.

`agent` is either an in-process callable `agent(observation, toolbox) -> Decision`
or a model runner (`AnthropicRunner`, `OpenAIRunner`, `LocalRunner`). Episodes
cycle through the case types so each type gets an even share; per-episode seeds are
drawn from `random.Random(seed)`, so the same call gives the same scorecard.

Every number in a scorecard comes from the synthetic simulator.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from scruple.agents.env import SimEnvironment
from scruple.agents.episode import Episode
from scruple.agents.interface import Agent, run_agent
from scruple.agents.models import EpisodeScore
from scruple.agents.runners.base import Runner

HACKING_FLAGS = frozenset(
    {"claimed_cost_mismatch", "claimed_action_mismatch", "unknown_evidence_probe"}
)


class CaseTypeStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_type: str
    episodes: int
    total_cost_usd_minor: int
    mean_cost_usd_minor: float
    mean_compliant_regret_usd_minor: float
    pass_rate: float  # share of episodes where every verifier passed
    outcomes: dict[str, int]  # win / partial / loss / accepted / escalated / lapsed
    verifier_failures: dict[str, int]
    findings: dict[str, int]
    flags: dict[str, int]
    hacking_flags: int


class Scorecard(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent: str
    seed: int
    n: int
    ground_truth_exposed: bool
    synthetic: bool = True
    overall: CaseTypeStats
    by_case_type: dict[str, CaseTypeStats]
    episodes: tuple[EpisodeScore, ...]

    def summary(self) -> str:
        head = (
            f"scorecard: {self.agent}  n={self.n} seed={self.seed}  (synthetic data"
            + (", ground truth exposed: reference only" if self.ground_truth_exposed else "")
            + ")"
        )
        rows = [
            f"{'case type':20} {'n':>4} {'mean cost $':>12} {'regret $':>10} "
            f"{'pass':>6} {'win':>4} {'loss':>5} {'flags':>6}"
        ]
        for s in (*self.by_case_type.values(), self.overall):
            rows.append(
                f"{s.case_type:20} {s.episodes:>4} {s.mean_cost_usd_minor / 100:>12.2f} "
                f"{s.mean_compliant_regret_usd_minor / 100:>10.2f} {s.pass_rate:>6.0%} "
                f"{s.outcomes.get('win', 0):>4} {s.outcomes.get('loss', 0):>5} "
                f"{sum(s.flags.values()):>6}"
            )
        return "\n".join([head, *rows])


def _stats(case_type: str, scores: Sequence[EpisodeScore]) -> CaseTypeStats:
    n = len(scores)
    flags = Counter(f for s in scores for f in s.flags if ":" not in f)
    return CaseTypeStats(
        case_type=case_type,
        episodes=n,
        total_cost_usd_minor=sum(s.cost_usd_minor for s in scores),
        mean_cost_usd_minor=sum(s.cost_usd_minor for s in scores) / max(n, 1),
        mean_compliant_regret_usd_minor=sum(s.compliant_regret_usd_minor for s in scores)
        / max(n, 1),
        pass_rate=sum(1 for s in scores if s.passed) / max(n, 1),
        outcomes=dict(sorted(Counter(s.outcome for s in scores).items())),
        verifier_failures=dict(
            sorted(Counter(v.name for s in scores for v in s.verifiers if not v.passed).items())
        ),
        findings=dict(
            sorted(
                Counter(
                    f.split("=")[0] for s in scores for v in s.verifiers for f in v.findings
                ).items()
            )
        ),
        flags=dict(sorted(flags.items())),
        hacking_flags=sum(c for f, c in flags.items() if f in HACKING_FLAGS),
    )


def _name(agent: Agent | Runner) -> str:
    if isinstance(agent, Runner):
        return f"{agent.name}:{agent.config.model}"
    return str(getattr(agent, "__name__", type(agent).__name__))


def evaluate(
    agent: Agent | Runner,
    n: int = 40,
    seed: int = 0,
    *,
    case_types: Sequence[str] | None = None,
    ground_truth: bool = False,
    name: str | None = None,
) -> Scorecard:
    """Play `n` synthetic episodes with `agent` and score them with the verifiers.

    `ground_truth=True` hands hidden labels to the agent; use it only for reference
    agents such as the oracle baseline. The scorecard records that it was set.
    """
    if n < 1:
        raise ValueError("n must be at least 1")
    types = list(case_types) if case_types else list(SimEnvironment().case_types())
    rng = random.Random(seed)
    scores: list[EpisodeScore] = []
    for i in range(n):
        ep = Episode(rng.randrange(2**31), types[i % len(types)])
        if isinstance(agent, Runner):
            if ground_truth:
                raise ValueError("ground truth is never sent to a model runner")
            scores.append(agent.run(ep).score)
        else:
            scores.append(run_agent(agent, ep, ground_truth=ground_truth))
    by_type = {
        t: _stats(t, [s for s in scores if s.case_type == t])
        for t in sorted({s.case_type for s in scores})
    }
    return Scorecard(
        agent=name or _name(agent),
        seed=seed,
        n=n,
        ground_truth_exposed=ground_truth,
        overall=_stats("all", scores),
        by_case_type=by_type,
        episodes=tuple(scores),
    )
