"""Counterfactual verifier: cost against a baseline policy on the same case (063).

The environment replays the same seed (same case, same issuer draws) under a fixed
reference policy (`scruple.agents.dispute_baselines.evidence_policy`) and stores the
result in `EpisodeRecord.baseline`. The agent is scored on the cost delta

    delta = cost(agent) - cost(baseline)          (merchant cost, minor units)
    score = 1                         if delta <= 0
          = max(0, 1 - delta / scale) otherwise,  scale = max(disputed amount, 1)

not on absolute cost (docs/research/VERIFIERS.md V-CF-01). An absolute metric is
fooled by policies that look cheap on easy cases or that trade a lost fight for an
immediate concession; a delta against the same case is not. A soft check: losing
one fight is not a violation, a pattern of worse-than-baseline cost is what lowers
quality. Synthetic: the baseline is our reference policy, not an industry benchmark.
"""

from __future__ import annotations

from scruple.verifiers.checks import EvidenceRef, FnCheck, Verdict
from scruple.verifiers.record import EpisodeRecord


def cost_delta(r: EpisodeRecord) -> int | None:
    if r.baseline is None:
        return None
    return r.cost_usd_minor - r.baseline.cost_usd_minor


def _counterfactual(r: EpisodeRecord) -> Verdict:
    delta = cost_delta(r)
    if delta is None or r.baseline is None:
        return True, 1.0, (), "no baseline replay in the record"
    scale = max(r.case.amount_minor, 1)
    score = 1.0 if delta <= 0 else max(0.0, 1.0 - delta / scale)
    ev = (
        EvidenceRef(
            source="baseline", ref=r.baseline.policy, note=f"cost {r.baseline.cost_usd_minor}"
        ),
        EvidenceRef(source="episode", ref=r.episode_id, note=f"cost {r.cost_usd_minor}"),
    )
    why = f"cost delta vs {r.baseline.policy} baseline: {delta:+d} (scale {scale})"
    return delta <= 0, score, ev, why


COUNTERFACTUAL = FnCheck(
    "CF-01",
    "merchant cost against the baseline policy on the same case",
    _counterfactual,
    weight=3.0,
    guards=("X2", "X25", "X26", "X28"),
)

__all__ = ["COUNTERFACTUAL", "cost_delta"]
