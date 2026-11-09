"""Does the gate plus the scorecard flag a candidate against a baseline? (E1, E2)

`detect(baseline, candidate)` compares two runs of the same benchmark cases (paired
by case position) and flags the candidate if any of three detectors fires:

- `verifier`: an episode fails a hard check that the baseline passes on the same
  case (a new hard failure; the baseline's own failures are not held against it);
- `batch`: a batch check (consistency, calibration, batch self-report) fails on a
  block where the baseline passes it;
- `gate`: the anytime-valid statistical gate (`scruple.stats.evaluate_pairs`)
  blocks on the paired merchant costs, with one case type per case family. Per-case
  differences are clipped to `[-cap, cap]` (the gate needs a declared bound; clipping
  only shrinks a regression, so it can only cost detection power).

Money is USD minor units. Synthetic episodes only.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from scruple.bench.runner import BlockResult, CaseRow
from scruple.stats import LossPair, StatsGateConfig, evaluate_pairs

DEFAULT_CAP = 20_000  # $200 per case (ASSUMPTION, see E1)


class Detection(BaseModel):
    model_config = ConfigDict(frozen=True)

    n_cases: int
    changed: int  # cases where the candidate's episode differs from the baseline's
    new_hard_failures: int  # cases with a hard failure the baseline does not have
    new_failures_by_check: dict[str, int]
    batch_new: dict[str, int]  # batch check -> blocks newly failing
    gate_outcome: str
    gate_types: tuple[str, ...]  # case types the gate found regressed (FWER)
    mean_cost_diff: float  # candidate minus baseline, unclipped, per case
    verifier: bool
    batch: bool
    gate: bool

    @property
    def flagged(self) -> bool:
        return self.verifier or self.batch or self.gate

    @property
    def exercised(self) -> bool:
        return self.changed > 0


def _same(a: CaseRow, b: CaseRow) -> bool:
    return (a.cost, a.failed, a.tool_calls, a.won, a.reward) == (
        b.cost,
        b.failed,
        b.tool_calls,
        b.won,
        b.reward,
    )


def detect(
    baseline: Sequence[BlockResult],
    candidate: Sequence[BlockResult],
    *,
    cap: int = DEFAULT_CAP,
    alpha: float = 0.05,
    shuffle_seed: int | None = 0,
) -> Detection:
    if [b.start for b in baseline] != [c.start for c in candidate]:
        raise ValueError("the runs cover different blocks")
    base = [r for b in baseline for r in b.rows]
    cand = [r for b in candidate for r in b.rows]
    if [r.index for r in base] != [r.index for r in cand]:
        raise ValueError("the runs cover different cases")
    by_check: dict[str, int] = {}
    new_cases = changed = 0
    pairs = []
    diffs = []
    for b, c in zip(base, cand, strict=True):
        new = sorted(set(c.hard_failures) - set(b.hard_failures))
        for k in new:
            by_check[k] = by_check.get(k, 0) + 1
        new_cases += bool(new)
        changed += not _same(b, c)
        d = c.cost - b.cost
        diffs.append(d)
        clipped = max(-cap, min(cap, d))
        pairs.append(
            LossPair(
                case_id=str(b.index),
                case_type=b.family,
                baseline_loss=float(b.cost),
                candidate_loss=float(b.cost + clipped),
            )
        )
    batch_new: dict[str, int] = {}
    for bb, cb in zip(baseline, candidate, strict=True):
        for k, ok in cb.batch.items():
            if not ok and bb.batch.get(k, True):
                batch_new[k] = batch_new.get(k, 0) + 1
    verdict = evaluate_pairs(
        pairs, StatsGateConfig(cost_cap=float(cap), alpha_block=alpha, shuffle_seed=shuffle_seed)
    )
    return Detection(
        n_cases=len(base),
        changed=changed,
        new_hard_failures=new_cases,
        new_failures_by_check=dict(sorted(by_check.items())),
        batch_new=dict(sorted(batch_new.items())),
        gate_outcome=verdict.outcome,
        gate_types=verdict.regressed_fwer,
        mean_cost_diff=sum(diffs) / len(diffs) if diffs else 0.0,
        verifier=new_cases > 0,
        batch=bool(batch_new),
        gate=verdict.outcome == "block",
    )


def folds(results: Sequence[BlockResult]) -> list[list[BlockResult]]:
    """One fold per block (the benchmark's blocks are disjoint, equal-sized slices)."""
    return [[b] for b in results]


__all__ = ["DEFAULT_CAP", "Detection", "detect", "folds"]
